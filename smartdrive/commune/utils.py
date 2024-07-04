import random

from communex._common import ComxSettings
from communex.client import CommuneClient

DEFAULT_NUM_CONNECTIONS = 1


def _try_get_client(url, num_connections):
    try:
        return CommuneClient(url, num_connections=num_connections)
    except Exception:
        return None


def get_comx_client(testnet: bool, num_connections: int = DEFAULT_NUM_CONNECTIONS) -> CommuneClient:
    comx_settings = ComxSettings()
    urls = comx_settings.TESTNET_NODE_URLS if testnet else comx_settings.NODE_URLS
    random.shuffle(urls)

    for url in urls:
        comx_client = _try_get_client(url, num_connections)
        if comx_client is not None:
            return comx_client

    raise Exception("No valid comx_client could be found")