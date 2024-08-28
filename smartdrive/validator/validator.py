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

import os
import argparse
import time
import asyncio

from communex.module.module import Module
from communex.compat.key import classic_load_key
from communex.types import Ss58Address
from substrateinterface import Keypair

import smartdrive
from smartdrive.commune.connection_pool import initialize_commune_connection_pool
from smartdrive.models.block import Block, MAX_EVENTS_PER_BLOCK
from smartdrive.validator.config import Config, config_manager
from smartdrive.validator.constants import TRUTHFUL_STAKE_AMOUNT
from smartdrive.validator.database.database import Database
from smartdrive.validator.api.api import API
from smartdrive.validator.evaluation.evaluation import score_miners, set_weights
from smartdrive.validator.node.active_validator_manager import INACTIVITY_TIMEOUT_SECONDS as VALIDATOR_INACTIVITY_TIMEOUT_SECONDS
from smartdrive.validator.models.models import ModuleType
from smartdrive.validator.node.node import Node
from smartdrive.validator.step import validate_step
from smartdrive.validator.utils import process_events, prepare_sync_blocks
from smartdrive.validator.api.middleware.sign import sign_data
from smartdrive.commune.request import get_filtered_modules, get_modules
from smartdrive.commune.utils import filter_truthful_validators


def get_config() -> Config:
    """
    Parse params and prepare config object.

    Returns:
        Config: Config object created from parser arguments.
    """
    path = os.path.abspath(__file__)
    db_path = os.path.join(os.path.dirname(path), "database")

    # Create parser and add all params.
    parser = argparse.ArgumentParser(description="Configure the validator.")
    parser.add_argument("--key", required=True, help="Name of key.")
    parser.add_argument("--database_path", default=db_path, required=False, help="Path to the database.")
    parser.add_argument("--port", type=int, default=8001, required=False, help="Default remote API port.")
    parser.add_argument("--testnet", action='store_true', help="Use testnet or not.")

    args = parser.parse_args()
    args.netuid = smartdrive.TESTNET_NETUID if args.testnet else smartdrive.NETUID

    if args.database_path:
        os.makedirs(args.database_path, exist_ok=True)

    args.database_path = os.path.expanduser(args.database_path)

    _config = Config(
        key=args.key,
        database_path=args.database_path,
        port=args.port,
        testnet=args.testnet,
        netuid=args.netuid
    )

    return _config


class Validator(Module):
    BLOCK_INTERVAL_SECONDS = 30
    VALIDATION_VOTE_INTERVAL_SECONDS = 2 * 60

    _config = None
    _key: Keypair = None
    _database: Database = None
    api: API = None
    node: Node = None
    _initial_sync_block = False

    def __init__(self):
        super().__init__()
        self._key = classic_load_key(config_manager.config.key)
        self._database = Database()
        self.node = Node()
        self.api = API(self.node)

    async def create_blocks(self):
        """
        Periodically attempts to create new blocks by proposing them to the network if the current node is the
        proposer.

        This method operates in an infinite loop, regularly checking whether it's time to vote, validate, or create
        a new block. The process includes validating the current validator's status, handling the initial sync,
        processing events, and ensuring that the block creation and validation intervals are respected.
        """
        last_validation_time = time.monotonic()

        while True:
            start_time = time.monotonic()

            try:
                if start_time - last_validation_time >= self.VALIDATION_VOTE_INTERVAL_SECONDS:
                    print("Starting validation and voting task")
                    asyncio.create_task(self.validation_task())
                    last_validation_time = start_time
            except Exception as e:
                print(f"Error validating - {e}")

            try:
                # Retrieving all active validators is crucial, so we attempt it an optimal number of times.
                # Between each attempt, we wait VALIDATOR_INACTIVITY_TIMEOUT_SECONDS / 2,
                # as new validators might be activated in the background.
                active_validators = []
                for _ in range(4):
                    active_validators = self.node.get_active_validators()
                    if active_validators:
                        break
                    await asyncio.sleep(VALIDATOR_INACTIVITY_TIMEOUT_SECONDS / 2)

                truthful_validators = filter_truthful_validators(active_validators)

                # Since the list of active validators never includes the current validator, we need to locate our own
                # validator within the complete list.
                all_validators = get_filtered_modules(config_manager.config.netuid, ModuleType.VALIDATOR)
                own_validator = next((v for v in all_validators if v.ss58_address == self._key.ss58_address), None)

                is_own_validator_truthful = own_validator and own_validator.stake >= TRUTHFUL_STAKE_AMOUNT
                if is_own_validator_truthful:
                    truthful_validators.append(own_validator)

                proposer_validator = max(truthful_validators or all_validators, key=lambda v: v.stake or 0)

                is_current_validator_proposer = proposer_validator.ss58_address == self._key.ss58_address
                if is_current_validator_proposer:
                    new_block_number = (self._database.get_last_block_number() or 0) + 1

                    # Trigger the initial sync and reiterate the loop after BLOCK_INTERVAL_SECONDS to verify if
                    # initial_sync_completed has been set to True. This is needed since the response to the
                    # prepare_sync_blocks will be in the background via TCP.
                    # TODO: Improve initial sync
                    if not _validator.node.initial_sync_completed.value:
                        _validator.node.initial_sync_completed.value = True
                        if active_validators:
                            prepare_sync_blocks(
                                start=new_block_number,
                                active_connections=self.node.get_active_validators_connections(),
                                keypair=self._key
                            )
                            await asyncio.sleep(self.BLOCK_INTERVAL_SECONDS)
                            continue

                    block_events = self.node.consume_pool_events(count=MAX_EVENTS_PER_BLOCK)
                    await process_events(
                        events=block_events,
                        is_proposer_validator=True,
                        keypair=self._key,
                        netuid=config_manager.config.netuid,
                        database=self._database
                    )

                    signed_block = sign_data({"block_number": new_block_number, "events": [event.dict() for event in block_events]}, self._key)
                    block = Block(
                        block_number=new_block_number,
                        events=block_events,
                        signed_block=signed_block.hex(),
                        proposer_ss58_address=Ss58Address(self._key.ss58_address)
                    )
                    self._database.create_block(block=block)

                    asyncio.create_task(self.node.send_block_to_validators(block=block))

                elapsed = time.monotonic() - start_time
                sleep_time = max(0.0, self.BLOCK_INTERVAL_SECONDS - elapsed)
                print(f"Sleeping for {sleep_time:.2f} seconds before trying to create the next block.")
                await asyncio.sleep(sleep_time)

            except Exception as e:
                print(f"Error creating blocks - {e}")
                await asyncio.sleep(self.BLOCK_INTERVAL_SECONDS)

    async def validation_task(self):
        """
        Handles the validation of events and updates the node's event pool accordingly.

        This method asynchronously processes events by validating them and then managing
        the resulting actions, such as removal, validation, or storage of events.
        """
        miners = [
            miner for miner in get_filtered_modules(config_manager.config.netuid, ModuleType.MINER)
            if miner.ss58_address != self._key.ss58_address
        ]

        remove_events, validation_events, result_miners = await validate_step(
            miners=miners,
            database=self._database,
            key=self._key,
            validators_len=len(self.node.get_active_validators_connections()) + 1  # To include myself
        )

        if result_miners:
            score_dict = score_miners(result_miners=result_miners)
            if _validator.node.initial_sync_completed.value and score_dict:
                await set_weights(score_dict, config_manager.config.netuid, self._key)

        if remove_events:
            await process_events(
                events=remove_events,
                is_proposer_validator=False,
                keypair=self._key,
                netuid=config_manager.config.netuid,
                database=self._database,
                is_temporary_chunk=True
            )

        if validation_events:
            self._database.insert_validation_events(validation_events=validation_events)

    async def periodically_ping_validators(self):
        """
        Periodically pings validators at regular intervals.

        This method runs an infinite loop that pings the node's validators
        every 5 seconds.
        """
        while True:
            await self.node.ping_validators()
            await asyncio.sleep(5)


if __name__ == "__main__":
    smartdrive.check_version()

    config = get_config()
    config_manager.initialize(config)

    initialize_commune_connection_pool(config_manager.config.testnet)

    key = classic_load_key(config_manager.config.key)
    registered_modules = get_modules(config_manager.config.netuid)
    if key.ss58_address not in [module.ss58_address for module in registered_modules]:
        raise Exception(f"Your key: {key.ss58_address} is not registered.")

    # Using an underscore to prevent naming conflicts with other variables later used named 'validator'
    _validator = Validator()

    async def run_tasks():
        asyncio.create_task(_validator.periodically_ping_validators())

        # Initial delay to allow active validators to load before request them
        await asyncio.sleep(VALIDATOR_INACTIVITY_TIMEOUT_SECONDS)

        await asyncio.gather(
            _validator.api.run_server(),
            _validator.create_blocks()
        )

    asyncio.run(run_tasks())
