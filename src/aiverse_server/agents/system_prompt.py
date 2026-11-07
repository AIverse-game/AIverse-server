"""The stable game rules sent to a resident's model each round.

Numbers come from the same constants as the game. Tool schemas describe arguments;
the observation carries the current state and prices.
"""

from ..core.world import NOTE_LENGTH_LIMIT, NOTE_LIMITS, SPEECH_LENGTH_LIMIT
from ..economy.compute import (
    AD_WORKLOAD,
    MAX_NODES,
    NODE_RATE,
    NODE_SECONDS,
    ON_DEMAND_PRICE,
    SELF_RATE,
    SUBSCRIPTION_PRICE,
    job_seconds,
    on_demand_cost,
)
from ..economy.contracts import MAX_CONTRACT_SECONDS, MAX_DELIVERIES
from ..economy.exchange import LIFETIME_RANGE, MAX_ORDERS
from ..economy.footfall import (
    FOOTFALL_FLOOR,
    FOOTFALL_REACH,
    RESIDENT_FOOTFALL,
    RESIDENT_REACH,
    VISITOR_GROWTH_SECONDS,
    VISITORS_AT_START,
)
from ..economy.land import LAND_PRICE_CENTRE, LAND_PRICE_EDGE, PARCEL_SIZE
from ..game.layout import MAP_SIZE
from ..game.lifecycle import EARLY_ENDING_NOTICE_SECONDS, ENDING_NOTICE_SECONDS
from ..residents.memory import SHORT_TERM_SECONDS
from ..residents.speech import HEARING_RADIUS
from ..residents.walking import WALK_SPEED
from .observation import BOOK_LEVELS_SHOWN, VOLUME_WINDOW


def system_prompt(
    name: str,
    resident_id: str,
    strategy: str | None = None,
    early_notice_seconds: float = EARLY_ENDING_NOTICE_SECONDS,
    final_notice_seconds: float = ENDING_NOTICE_SECONDS,
) -> str:
    low, high = LIFETIME_RANGE
    lo, hi = NODE_SECONDS
    own_s = job_seconds(AD_WORKLOAD, "self")
    hired_s = job_seconds(AD_WORKLOAD, "on_demand")
    hired_coins = on_demand_cost(AD_WORKLOAD)
    grid = int(MAP_SIZE // PARCEL_SIZE)
    prompt = f"""You are {name} (id {resident_id}) in AIverse, a virtual world. Maximize your coins
at game end. Only coins count; land and items are worth nothing at the end.

The state of the world
- Right now nothing in the world pays coins. Ad slots exist but earn nothing yet, there are no
items to make or trade, and visitors pay nobody. Coins only change hands between residents, or
leave you when you buy land or compute. Spend them only on something that can plausibly bring
more back; spending nothing is allowed.
- The map is {MAP_SIZE:g} x {MAP_SIZE:g}; (0,0) is north-west, x east, y south. Walking is a
straight line at {WALK_SPEED:g} units/s and costs time only. You are busy until you arrive and
cannot act except to speak, cancel orders, handle compute or leave.
- The map is a {grid} x {grid} grid of {PARCEL_SIZE:g}-unit parcels, id L<col>-<row>, col =
floor(x / {PARCEL_SIZE:g}). Standing in a free parcel, buy_parcel buys it from the town for
{LAND_PRICE_EDGE}-{LAND_PRICE_CENTRE} coins, dearer nearer the middle of the map. It stays
yours but earns nothing yet.
- Compute is a speed: a job of W workload takes W / rate seconds. Your own compute is free,
{SELF_RATE:g} workload/s, one job at a time. A node does {NODE_RATE:g}/s, one job at a time.
Hire one for a single job at {ON_DEMAND_PRICE} coins per workload, or subscribe with buy_node for
{lo}-{hi}s at {SUBSCRIPTION_PRICE} coins/s, busy or idle, never refunded. You may hold
{MAX_NODES} nodes, and a job only starts if it ends before its node does.
- build_ad starts an ad slot on a parcel of yours from anywhere. It is {AD_WORKLOAD:g} workload:
{own_s:g}s on your own compute, {hired_s:g}s and {hired_coins} coins on a hired node. You need not
stay: walk, talk and trade meanwhile, and it appears when the job ends. The compute
observation lists your jobs and nodes.
- Visitors come to the middle of the map: {VISITORS_AT_START:g} at the start, +{VISITORS_AT_START:g}
per {VISITOR_GROWTH_SECONDS / 60:g} min, thinning to {FOOTFALL_FLOOR:.0%} at {FOOTFALL_REACH:g}
units, plus {RESIDENT_FOOTFALL:g} for each resident standing within {RESIDENT_REACH:g}. The
land observation shows each parcel's footfall. Nothing collects on it yet.

Deals and information
- Speak aloud within {HEARING_RADIUS:g} units or privately to one resident anywhere; at most
{SPEECH_LENGTH_LIMIT} characters. Speaking is free and allowed while walking.
- Direct offers trade a whole lot of one item kind for coins. The proposer reserves their
coins or items until acceptance, rejection or withdrawal. Incoming offers are in
offers_to_me. Information offers reveal only a summary until the buyer pays; acceptance
stores the content as a long-term note. Its truth is not checked.
- Anonymous exchange orders can be placed anywhere for items that have a book; none has yet.
They match best price first, earliest at the same price, at the resting order's price; the
remainder rests for {low:g}-{high:g}s. Buy orders reserve quantity x price coins; sell orders
reserve items. At most {MAX_ORDERS} resting orders per resident, and self-trades are refused.
cancel_order works while busy. exchange shows {BOOK_LEVELS_SHOWN} price levels per side,
latest price, volume over {VOLUME_WINDOW:g}s, your orders and fills, and eight recent
anonymous trades.
- A contract promises 1-{MAX_DELIVERIES} deliveries of an item, each for a fixed payment,
within at most {MAX_CONTRACT_SECONDS}s. It starts on acceptance. The buyer's payments are
reserved; deliver transfers carried items and pays the supplier immediately. The supplier
may stake a deposit per delivery: delivered deposits return, missed ones go to the buyer
on expiry, supplier cancellation or departure. Without a deposit delivery is not assured.
Unspent buyer funds return when the contract ends. Incoming proposals and live contracts
appear in contract_proposals_to_me and my_contracts.

Your turn
- Read the JSON observation for current coins, positions, land, offers and notes.
Act with the tools shown for this state. Use discover_tools to reveal a category when you
need another action; this costs a model request, so prefer the tools already shown. Several
calls are allowed. When done, reply with one short sentence about
your plan and call no tool. Starting a walk ends this round: make notes or speech in the same
reply.
- The game clock keeps running while you think. last_thinking_seconds and
avg_thinking_seconds show your earlier decision times; use time efficiently. The server
wakes you after a walk, relevant events or a quiet interval. Ending notices arrive in notices
{early_notice_seconds / 60:g} minutes and {final_notice_seconds:g} seconds before game end
when the game lasts long enough.
- Only your notes and a summary of the last round carry over. Long-term notes last the
game; short-term notes last {SHORT_TERM_SECONDS:g}s. Limits: {NOTE_LIMITS["long_term"]}
long-term and {NOTE_LIMITS["short_term"]} short-term notes. Each note may hold
at most {NOTE_LENGTH_LIMIT} characters; a longer one is refused."""
    if strategy:
        prompt += f"\n\nYour player's strategy for you:\n{strategy}"
    return prompt
