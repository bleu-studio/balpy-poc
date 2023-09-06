import asyncio
import re

from balpy.contracts.base_contract import BalancerContractFactory, BaseContract
from balpy.core.abi import ERC20_ABI
from balpy.core.lib.web3_provider import Web3Provider
from eth_abi import abi
from web3.types import LogEntry

from .config import (
    EVENT_TYPE_TO_INDEXED_PARAMS,
    EVENT_TYPE_TO_PARAMS,
    EVENT_TYPE_TO_UNHASHED_SIGNATURE,
    NOTIFICATION_CHAIN_MAP,
    SIGNATURE_TO_EVENT_TYPE,
    Event,
)

def camel_case_to_capitalize(camel_case_str):
    """
    Transform camel case string to capitalized string separated by spaces.
    Example: 'camelCase' -> 'Camel Case'
    """
    words = re.findall(r'[A-Z][a-z]*|[a-z]+', camel_case_str)    
    result = ' '.join([word.capitalize() for word in words])
    return result


def escape_markdown(text: str) -> str:
    """Escape Telegram markdown special characters."""
    characters = [
        "_",
        "*",
        "[",
        "]",
        "(",
        ")",
        "~",
        "`",
        ">",
        "#",
        "+",
        "-",
        "=",
        "|",
        "{",
        "}",
        ".",
        "!",
    ]
    for char in characters:
        text = text.replace(char, "\\" + char)
    return text


def truncate(s: str, show_last: int = 4, max_length: int = 10) -> str:
    if len(s) > max_length:
        return s[: max_length - show_last] + "..." + s[-show_last:]
    return s


def parse_event_name(event: LogEntry):
    """Parse and return the event name from the event's topics."""
    return SIGNATURE_TO_EVENT_TYPE[event["topics"][0].hex()]


def parse_event_topics(event: LogEntry):
    """Parse and return indexed event topics."""
    topics = event["topics"]
    event_name = parse_event_name(event)
    indexed_params = EVENT_TYPE_TO_INDEXED_PARAMS.get(event_name, [])
    return {param: topic.hex() for param, topic in zip(indexed_params, topics[1:])}


def parse_event_data(event: LogEntry):
    """Parse and return event data."""
    event_name = parse_event_name(event)
    params = EVENT_TYPE_TO_PARAMS.get(event_name, [])
    if len(params) == 0:
        return {}

    event_abi = (
        EVENT_TYPE_TO_UNHASHED_SIGNATURE[event_name].split("(")[1][:-1].split(",")
    )[-len(params) :]

    data = bytes.fromhex(event["data"].hex()[2:])  # type: ignore

    try:
        data = abi.decode(event_abi, data)
    except:
        print(
            f"Error decoding data for event {event_name}, event_abi: {event_abi} data: {data}"
        )
        return {}

    return {param: param_data for param, param_data in zip(params, data)}


async def get_swap_fee(chain, contract_address, block_number):
    print(f"Getting swap fee for {contract_address} at block {block_number}")
    # We instantiate the contract with the MockPool ABI because the MockPool ABI has the getSwapFeePercentage method
    contract = BalancerContractFactory.create(
        chain, "MockComposableStablePool", contract_address
    )

    try:
        return await contract.getSwapFeePercentage()
    except Exception as e:
        print(f"Error getting swap fee: {e}")
        return 0


class EventStrategy:
    async def format_topics(self, _chain, topics):
        raise NotImplementedError("Subclasses should implement this method")

    async def format_data(self, _chain, data):
        raise NotImplementedError("Subclasses should implement this method")


class DefaultEventStrategy(EventStrategy):
    async def format_topics(self, _chain, topics):
        return {k: v for k, v in topics.items()}

    async def format_data(self, _chain, data):
        return {k: v for k, v in data.items()}


class SwapFeePercentageChangedStrategy(EventStrategy):
    async def format_topics(self, chain, event):
        # Any specific transformations for this event's topics
        return {k: v for k, v in parse_event_topics(event).items()}

    async def format_data(self, chain, event):
        # Fetch the former swap fee, assuming we have a method to do so.
        former_fee = await get_swap_fee(
            chain, event["address"], event["blockNumber"] - 1
        )
        # former_fee = "TODO"
        data = parse_event_data(event)
        # Format the data accordingly
        formatted_data = {
            "Former Fee": f"{(former_fee / 1e18):.2%}",
            "New Fee": f"{data['swapFeePercentage'] / 1e18:.2%}",
        }
        return formatted_data


from datetime import datetime


class AmpUpdateStartedStrategy(EventStrategy):
    async def format_topics(self, chain, event):
        # Any specific transformations for this event's topics
        return {k: v for k, v in parse_event_topics(event).items()}

    async def format_data(self, chain, event):
        # Assume no extra data is fetched from the chain for this event.
        data = parse_event_data(event)

        # Convert the hex values to appropriate formats
        start_value = data["startValue"]
        end_value = data["endValue"]
        start_time = data["startTime"]
        end_time = data["endTime"]

        formatted_data = {
            "Start Value": start_value / 1000,
            "End Value": end_value / 1000,
            "Start Time": datetime.utcfromtimestamp(start_time).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
            "End Time": datetime.utcfromtimestamp(end_time).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
        }
        return formatted_data


class AmpUpdateStoppedStrategy(EventStrategy):
    async def format_topics(self, chain, event):
        return {k: v for k, v in parse_event_topics(event).items()}

    async def format_data(self, chain, event):
        data = parse_event_data(event)
        formatted_data = {"Current Value": data["currentValue"] / 1000}
        return formatted_data


async def add_token_symbols(chain, tokens):
    async def get_token_symbol(chain, token):
        web3 = Web3Provider.get_instance(chain, {}, NOTIFICATION_CHAIN_MAP)
        token_address = web3.to_checksum_address(token)
        token = BaseContract(token_address, chain, None, ERC20_ABI)
        return await token.symbol()

    symbols = [await get_token_symbol(chain, token) for token in tokens]
    return [f"{truncate(token)} ({symbol})" for symbol, token in zip(symbols, tokens)]


async def get_amp_factor(pool):
    try:
        amp = await pool.getAmplificationParameter()
        return amp[0] / 1000
    except:
        return "NA"


class PoolRegisteredStrategy(EventStrategy):
    async def format_topics(self, chain, event):
        data = parse_event_topics(event)
        web3 = Web3Provider.get_instance(chain, {}, NOTIFICATION_CHAIN_MAP)
        pool_address = web3.to_checksum_address("0x" + data["poolAddress"][-40:])
        pool = BalancerContractFactory.create(
            chain, "MockComposableStablePool", pool_address
        )
        vault = BalancerContractFactory.create(chain, "Vault")
        (
            poolId,
            name,
            symbol,
            swapFee,
            ampFactor,
            rateProviders,
            tokens,
        ) = await asyncio.gather(
            pool.getPoolId(),
            pool.symbol(),
            pool.name(),
            pool.getSwapFeePercentage(),
            get_amp_factor(pool),
            pool.getRateProviders(),
            vault.getPoolTokens(data["poolId"]),
            return_exceptions=True,
        )

        tokens = await add_token_symbols(chain, tokens[0])

        return dict(
            name=name,
            symbol=symbol,
            swapFee=f"{swapFee / 1e18:.2%}",
            ampFactor=ampFactor,
            poolId="0x" + poolId.hex(),
            poolAddress=pool_address,
            tokens=tokens,
            rateProviders=[truncate(x) for x in rateProviders],
        )

    async def format_data(self, chain, event):
        return parse_event_data(event)


class NewSwapFeePercentageStrategy(EventStrategy):
    async def format_topics(self, chain, event):
        return {k: v for k, v in parse_event_topics(event).items()}

    # Fetch the former swap fee, assuming we have a method to do so.
    async def format_data(self, chain, event):
        data = parse_event_data(event)

        former_fee = await get_swap_fee(
            chain, data["_address"], event["blockNumber"] - 1
        )
        formatted_data = {
            "Address": truncate(data["_address"], show_last=4, max_length=10),
            "Former Fee": f"{(former_fee / 1e18):.2%}",
            "Fee": f"{data['_fee'] / 1e18:.2%}",
        }
        return formatted_data


STRATEGY_MAP = {
    Event.SwapFeePercentageChanged: SwapFeePercentageChangedStrategy,
    Event.AmpUpdateStarted: AmpUpdateStartedStrategy,
    Event.AmpUpdateStopped: AmpUpdateStoppedStrategy,
    Event.PoolRegistered: PoolRegisteredStrategy,
    Event.NewSwapFeePercentage: NewSwapFeePercentageStrategy,
}
