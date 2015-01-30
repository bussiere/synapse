# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from twisted.internet import defer

from .units import Edu

from synapse.util.logutils import log_function
from synapse.events import FrozenEvent
from synapse.events.utils import prune_event

from syutil.jsonutil import encode_canonical_json

from synapse.crypto.event_signing import check_event_content_hash

from synapse.api.errors import SynapseError

import logging


logger = logging.getLogger(__name__)


class FederationClient(object):
    @log_function
    def send_pdu(self, pdu, destinations):
        """Informs the replication layer about a new PDU generated within the
        home server that should be transmitted to others.

        TODO: Figure out when we should actually resolve the deferred.

        Args:
            pdu (Pdu): The new Pdu.

        Returns:
            Deferred: Completes when we have successfully processed the PDU
            and replicated it to any interested remote home servers.
        """
        order = self._order
        self._order += 1

        logger.debug("[%s] transaction_layer.enqueue_pdu... ", pdu.event_id)

        # TODO, add errback, etc.
        self._transaction_queue.enqueue_pdu(pdu, destinations, order)

        logger.debug(
            "[%s] transaction_layer.enqueue_pdu... done",
            pdu.event_id
        )

    @log_function
    def send_edu(self, destination, edu_type, content):
        edu = Edu(
            origin=self.server_name,
            destination=destination,
            edu_type=edu_type,
            content=content,
        )

        # TODO, add errback, etc.
        self._transaction_queue.enqueue_edu(edu)
        return defer.succeed(None)

    @log_function
    def send_failure(self, failure, destination):
        self._transaction_queue.enqueue_failure(failure, destination)
        return defer.succeed(None)

    @log_function
    def make_query(self, destination, query_type, args,
                   retry_on_dns_fail=True):
        """Sends a federation Query to a remote homeserver of the given type
        and arguments.

        Args:
            destination (str): Domain name of the remote homeserver
            query_type (str): Category of the query type; should match the
                handler name used in register_query_handler().
            args (dict): Mapping of strings to strings containing the details
                of the query request.

        Returns:
            a Deferred which will eventually yield a JSON object from the
            response
        """
        return self.transport_layer.make_query(
            destination, query_type, args, retry_on_dns_fail=retry_on_dns_fail
        )

    @defer.inlineCallbacks
    @log_function
    def backfill(self, dest, context, limit, extremities):
        """Requests some more historic PDUs for the given context from the
        given destination server.

        Args:
            dest (str): The remote home server to ask.
            context (str): The context to backfill.
            limit (int): The maximum number of PDUs to return.
            extremities (list): List of PDU id and origins of the first pdus
                we have seen from the context

        Returns:
            Deferred: Results in the received PDUs.
        """
        logger.debug("backfill extrem=%s", extremities)

        # If there are no extremeties then we've (probably) reached the start.
        if not extremities:
            return

        transaction_data = yield self.transport_layer.backfill(
            dest, context, extremities, limit)

        logger.debug("backfill transaction_data=%s", repr(transaction_data))

        pdus = [
            self.event_from_pdu_json(p, outlier=False)
            for p in transaction_data["pdus"]
        ]

        for i, pdu in enumerate(pdus):
            pdus[i] = yield self._check_sigs_and_hash(pdu)

            # FIXME: We should handle signature failures more gracefully.

        defer.returnValue(pdus)

    @defer.inlineCallbacks
    @log_function
    def get_pdu(self, destinations, event_id, outlier=False):
        """Requests the PDU with given origin and ID from the remote home
        servers.

        Will attempt to get the PDU from each destination in the list until
        one succeeds.

        This will persist the PDU locally upon receipt.

        Args:
            destinations (list): Which home servers to query
            pdu_origin (str): The home server that originally sent the pdu.
            event_id (str)
            outlier (bool): Indicates whether the PDU is an `outlier`, i.e. if
                it's from an arbitary point in the context as opposed to part
                of the current block of PDUs. Defaults to `False`

        Returns:
            Deferred: Results in the requested PDU.
        """

        # TODO: Rate limit the number of times we try and get the same event.

        pdu = None
        for destination in destinations:
            try:
                transaction_data = yield self.transport_layer.get_event(
                    destination, event_id
                )

                logger.debug("transaction_data %r", transaction_data)

                pdu_list = [
                    self.event_from_pdu_json(p, outlier=outlier)
                    for p in transaction_data["pdus"]
                ]

                if pdu_list:
                    pdu = pdu_list[0]

                    # Check signatures are correct.
                    pdu = yield self._check_sigs_and_hash(pdu)

                    break

            except Exception as e:
                logger.info(
                    "Failed to get PDU %s from %s because %s",
                    event_id, destination, e,
                )
                continue

        defer.returnValue(pdu)

    @defer.inlineCallbacks
    @log_function
    def get_state_for_room(self, destination, room_id, event_id):
        """Requests all of the `current` state PDUs for a given room from
        a remote home server.

        Args:
            destination (str): The remote homeserver to query for the state.
            room_id (str): The id of the room we're interested in.
            event_id (str): The id of the event we want the state at.

        Returns:
            Deferred: Results in a list of PDUs.
        """

        result = yield self.transport_layer.get_room_state(
            destination, room_id, event_id=event_id,
        )

        pdus = [
            self.event_from_pdu_json(p, outlier=True) for p in result["pdus"]
        ]

        auth_chain = [
            self.event_from_pdu_json(p, outlier=True)
            for p in result.get("auth_chain", [])
        ]

        for i, pdu in enumerate(pdus):
            pdus[i] = yield self._check_sigs_and_hash(pdu)

            # FIXME: We should handle signature failures more gracefully.

        for i, pdu in enumerate(auth_chain):
            auth_chain[i] = yield self._check_sigs_and_hash(pdu)

            # FIXME: We should handle signature failures more gracefully.

        defer.returnValue((pdus, auth_chain))

    @defer.inlineCallbacks
    @log_function
    def get_event_auth(self, destination, room_id, event_id):
        res = yield self.transport_layer.get_event_auth(
            destination, room_id, event_id,
        )

        auth_chain = [
            self.event_from_pdu_json(p, outlier=True)
            for p in res["auth_chain"]
        ]

        for i, pdu in enumerate(auth_chain):
            auth_chain[i] = yield self._check_sigs_and_hash(pdu)

            # FIXME: We should handle signature failures more gracefully.

        auth_chain.sort(key=lambda e: e.depth)

        defer.returnValue(auth_chain)

    @defer.inlineCallbacks
    def make_join(self, destination, room_id, user_id):
        ret = yield self.transport_layer.make_join(
            destination, room_id, user_id
        )

        pdu_dict = ret["event"]

        logger.debug("Got response to make_join: %s", pdu_dict)

        defer.returnValue(self.event_from_pdu_json(pdu_dict))

    @defer.inlineCallbacks
    def send_join(self, destination, pdu):
        time_now = self._clock.time_msec()
        _, content = yield self.transport_layer.send_join(
            destination=destination,
            room_id=pdu.room_id,
            event_id=pdu.event_id,
            content=pdu.get_pdu_json(time_now),
        )

        logger.debug("Got content: %s", content)

        state = [
            self.event_from_pdu_json(p, outlier=True)
            for p in content.get("state", [])
        ]

        auth_chain = [
            self.event_from_pdu_json(p, outlier=True)
            for p in content.get("auth_chain", [])
        ]

        for i, pdu in enumerate(state):
            state[i] = yield self._check_sigs_and_hash(pdu)

            # FIXME: We should handle signature failures more gracefully.

        for i, pdu in enumerate(auth_chain):
            auth_chain[i] = yield self._check_sigs_and_hash(pdu)

            # FIXME: We should handle signature failures more gracefully.

        auth_chain.sort(key=lambda e: e.depth)

        defer.returnValue({
            "state": state,
            "auth_chain": auth_chain,
        })

    @defer.inlineCallbacks
    def send_invite(self, destination, room_id, event_id, pdu):
        time_now = self._clock.time_msec()
        code, content = yield self.transport_layer.send_invite(
            destination=destination,
            room_id=room_id,
            event_id=event_id,
            content=pdu.get_pdu_json(time_now),
        )

        pdu_dict = content["event"]

        logger.debug("Got response to send_invite: %s", pdu_dict)

        pdu = self.event_from_pdu_json(pdu_dict)

        # Check signatures are correct.
        pdu = yield self._check_sigs_and_hash(pdu)

        # FIXME: We should handle signature failures more gracefully.

        defer.returnValue(pdu)

    @defer.inlineCallbacks
    def query_auth(self, destination, room_id, event_id, local_auth):
        """
        Params:
            destination (str)
            event_it (str)
            local_auth (list)
        """
        time_now = self._clock.time_msec()

        send_content = {
            "auth_chain": [e.get_pdu_json(time_now) for e in local_auth],
        }

        code, content = yield self.transport_layer.send_query_auth(
            destination=destination,
            room_id=room_id,
            event_id=event_id,
            content=send_content,
        )

        auth_chain = [
            (yield self._check_sigs_and_hash(self.event_from_pdu_json(e)))
            for e in content["auth_chain"]
        ]

        ret = {
            "auth_chain": auth_chain,
            "rejects": content.get("rejects", []),
            "missing": content.get("missing", []),
        }

        defer.returnValue(ret)

    def event_from_pdu_json(self, pdu_json, outlier=False):
        event = FrozenEvent(
            pdu_json
        )

        event.internal_metadata.outlier = outlier

        return event

    @defer.inlineCallbacks
    def _check_sigs_and_hash(self, pdu):
        """Throws a SynapseError if the PDU does not have the correct
        signatures.

        Returns:
            FrozenEvent: Either the given event or it redacted if it failed the
            content hash check.
        """
        # Check signatures are correct.
        redacted_event = prune_event(pdu)
        redacted_pdu_json = redacted_event.get_pdu_json()

        try:
            yield self.keyring.verify_json_for_server(
                pdu.origin, redacted_pdu_json
            )
        except SynapseError:
            logger.warn(
                "Signature check failed for %s redacted to %s",
                encode_canonical_json(pdu.get_pdu_json()),
                encode_canonical_json(redacted_pdu_json),
            )
            raise

        if not check_event_content_hash(pdu):
            logger.warn(
                "Event content has been tampered, redacting %s, %s",
                pdu.event_id, encode_canonical_json(pdu.get_dict())
            )
            defer.returnValue(redacted_event)

        defer.returnValue(pdu)