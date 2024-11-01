#  MIT License
#
#  Copyright (c) 2024 Dezen | freedom block by block
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy
#  of this software and associated documentation files (the "Software"), to deal
#  in the Software without restriction, including without limitation the rights
#  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#  copies of the Software, and to permit persons to whom the Software is
#  furnished to do so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in all
#  copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#  SOFTWARE.

import asyncio

from communex.balance import from_nano
from communex.types import Ss58Address

import smartdrive
from smartdrive import logger
from smartdrive.commune.request import get_staketo
from smartdrive.commune.models import ModuleInfo

INITIAL_STORAGE = 50 * 1024 * 1024  # 50 MB
MAXIMUM_STORAGE = 2 * 1024 * 1024 * 1024  # 2 GB
ADDITIONAL_STORAGE_PER_COMAI = 0.1 * 1024 * 1024  # 0.1 MB
MINIMUM_STAKE = 1  # 1 COMAI

DEFAULT_MINER_PATH = "~/.smartdrive/miner"
DEFAULT_VALIDATOR_PATH = "~/.smartdrive/validator"
DEFAULT_CLIENT_PATH = "~/.smartdrive/client"

INTERVAL_CHECK_VERSION_SECONDS = 12 * 60 * 60  # 12 hours


def calculate_storage_capacity(stake: float) -> int:
    """
    Calculates the storage capacity based on the user's stake,
    with a maximum limit of MAXIMUM_STORAGE.

    Params:
        stake (float): The current user's stake in COMAI.

    Returns:
        int: The total storage capacity in bytes, capped at MAXIMUM_STORAGE.
    """
    if stake < MINIMUM_STAKE:
        return 0

    total_storage_bytes = INITIAL_STORAGE

    additional_comai = stake - MINIMUM_STAKE
    if additional_comai > 0:
        total_storage_bytes += additional_comai * ADDITIONAL_STORAGE_PER_COMAI

    # Limit the total storage to MAXIMUM_STORAGE in bytes
    return int(min(total_storage_bytes, MAXIMUM_STORAGE))


def format_size(size_in_bytes: int) -> str:
    """
    Format the size from bytes to a human-readable format (MB or GB).

    Params:
        size_in_bytes (int): The size in bytes.

    Returns:
        str: The size formatted in MB or GB.
    """
    size_in_mb = size_in_bytes / (1024 * 1024)
    if size_in_mb >= 1024:
        size_in_gb = size_in_mb / 1024
        return f"{size_in_gb:.2f} GB"
    else:
        return f"{size_in_mb:.2f} MB"


async def get_stake_from_user(user_ss58_address: Ss58Address, validators: [ModuleInfo]):
    staketo_modules = await get_staketo(user_ss58_address)
    validator_addresses = {validator.ss58_address for validator in validators}
    active_stakes = {address: from_nano(stake) for address, stake in staketo_modules.items() if address in validator_addresses and address != str(user_ss58_address)}

    return sum(active_stakes.values())


async def periodic_version_check():
    while True:
        logger.info("Checking for updates...")
        smartdrive.check_version()
        await asyncio.sleep(INTERVAL_CHECK_VERSION_SECONDS)
