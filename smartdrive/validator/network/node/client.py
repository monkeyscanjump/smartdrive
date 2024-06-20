# MIT License
#
# Copyright (c) 2024 Dezen | freedom block by block
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import asyncio
import multiprocessing
from typing import List

from communex._common import get_node_url
from communex.client import CommuneClient
from communex.compat.key import classic_load_key

from smartdrive.models.event import parse_event, UserEvent, MessageEvent, Action, Event
from smartdrive.validator.api.middleware.sign import verify_data_signature
from smartdrive.validator.api.middleware.subnet_middleware import get_ss58_address_from_public_key
from smartdrive.validator.api.utils import process_events
from smartdrive.validator.config import config_manager
from smartdrive.validator.database.database import Database
from smartdrive.models.block import BlockEvent, block_event_to_block
from smartdrive.validator.network.node.connection_pool import ConnectionPool
from smartdrive.validator.network.node.util import packing
from smartdrive.validator.network.node.util.exceptions import MessageException, ClientDisconnectedException, MessageFormatException, InvalidSignatureException
from smartdrive.validator.network.node.util.message_code import MessageCode


class Client(multiprocessing.Process):

    def __init__(self, client_socket, identifier, connection_pool: ConnectionPool, mempool):
        multiprocessing.Process.__init__(self)
        self.client_socket = client_socket
        self.identifier = identifier
        self.connection_pool = connection_pool
        self.mempool = mempool
        self.keypair = classic_load_key(config_manager.config.key)
        self.comx_client = CommuneClient(url=get_node_url(use_testnet=config_manager.config.testnet))
        self.database = Database()

    def run(self):
        try:
            self.handle_client()
        except ClientDisconnectedException:
            print(f"Removing connection from connection pool: {self.identifier}")
            removed_connection = self.connection_pool.remove_connection(self.identifier)
            if removed_connection:
                removed_connection.close()

    def handle_client(self):
        try:
            while True:
                self.receive()
        except InvalidSignatureException:
            print("Received invalid sign")
        except (MessageException, MessageFormatException):
            print(f"Received undecodable or invalid message: {self.identifier}")
        except (ConnectionResetError, ConnectionAbortedError, ClientDisconnectedException):
            print(f"Client disconnected': {self.identifier}")
        finally:
            self.client_socket.close()
            raise ClientDisconnectedException(f"Lost {self.identifier}")

    def receive(self):
        # Here the process is waiting till a new message is sent.
        msg = packing.receive_msg(self.client_socket)
        # Although mempool is managed by multiprocessing.Manager(),
        # we explicitly pass it as parameters to make it clear that it is dependency of the process_message process.
        process = multiprocessing.Process(target=self.process_message, args=(msg, self.mempool,))
        process.start()

    def process_message(self, msg, mempool):
        body = msg["body"]

        try:
            if body['code'] in [code.value for code in MessageCode]:
                signature_hex = msg["signature_hex"]
                public_key_hex = msg["public_key_hex"]
                ss58_address = get_ss58_address_from_public_key(public_key_hex)

                is_verified_signature = verify_data_signature(body, signature_hex, ss58_address)

                if not is_verified_signature:
                    raise InvalidSignatureException()

                if body['code'] == MessageCode.MESSAGE_CODE_BLOCK.value:
                    block_event = BlockEvent(
                        block_number=body["data"]["block_number"],
                        events=list(map(lambda event: MessageEvent.from_json(event["event"], Action(event["event_action"])), body["data"]["events"])),
                        proposer_signature=body["data"]["proposer_signature"],
                        proposer_ss58_address=body["data"]["proposer_ss58_address"]
                    )
                    block = block_event_to_block(block_event)

                    if not verify_data_signature(
                            data={"block_number": block.block_number, "events": [event.dict() for event in block.events]},
                            signature_hex=block.proposer_signature,
                            ss58_address=block.proposer_ss58_address
                    ):
                        print("Block not verified")
                        return

                    processed_events = []
                    for event in block.events:
                        input_params_verified = True
                        if isinstance(event, UserEvent):
                            input_params_verified = verify_data_signature(event.input_params.dict(), event.input_signed_params, event.user_ss58_address)

                        event_params_verified = verify_data_signature(event.event_params.dict(), event.event_signed_params, event.validator_ss58_address)

                        if input_params_verified and event_params_verified:
                            processed_events.append(event)

                    block.events = processed_events
                    self.run_process_events(processed_events)
                    self.remove_events(processed_events, mempool)
                    self.database.create_block(block=block)

                elif body['code'] == MessageCode.MESSAGE_CODE_EVENT.value:
                    message_event = MessageEvent.from_json(body["data"]["event"], Action(body["data"]["event_action"]))
                    event = parse_event(message_event)
                    mempool.append(event)

        except InvalidSignatureException as e:
            raise e

        except Exception as e:
            print(e)
            raise MessageFormatException('%s' % e)

    def run_process_events(self, processed_events):
        async def _run_process_events():
            await process_events(
                events=processed_events,
                is_proposer_validator=False,
                keypair=self.keypair,
                comx_client=self.comx_client,
                netuid=config_manager.config.netuid,
                database=self.database
            )

        loop = asyncio.get_event_loop()
        loop.run_until_complete(_run_process_events())

    def remove_events(self, events: List[Event], mempool):
        uuids_to_remove = {event.uuid for event in events}
        with multiprocessing.Lock():
            updated_mempool = [event for event in mempool if event.uuid not in uuids_to_remove]
            mempool[:] = updated_mempool

