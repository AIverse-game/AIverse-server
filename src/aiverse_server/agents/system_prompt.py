"""The stable game rules sent to a resident's model each round.

Numbers come from the same constants as the game. Tool schemas describe arguments;
the observation carries the current state and prices.
"""

from ..core.land import AD_SLOT_CAPACITY, PARCEL_SHARES
from ..core.world import NOTE_LENGTH_LIMIT, NOTE_LIMITS, SPEECH_LENGTH_LIMIT
from ..economy.admissions import FEES
from ..economy.ads import CONTENT_ITEM, CONTENT_LIFETIME
from ..economy.compute import (
    AD_WORKLOAD,
    CONTENT_WORKLOAD,
    GAME_WORKLOAD,
    MAX_NODES,
    NODE_RATE,
    NODE_SECONDS,
    ON_DEMAND_PRICE,
    SELF_RATE,
    SUBSCRIPTION_PRICE,
    VENUE_WORKLOAD,
    job_seconds,
    on_demand_cost,
)
from ..economy.contracts import MAX_CONTRACT_SECONDS, MAX_DELIVERIES
from ..economy.exchange import LIFETIME_RANGE, MAX_ORDERS
from ..economy.land import PARCEL_SIZE, plots
from ..economy.sponsors import PAY_PERIOD
from ..economy.visitors import MIN_VIEW_SECONDS, ROUTE_SHARE, VIEW_RADIUS, VISITOR_COUNT
from ..game.layout import BLOCK_CELLS, BLOCKS, MAP_SIZE, create_default_world
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
    content_own_s = job_seconds(CONTENT_WORKLOAD, "self")
    content_hired_s = job_seconds(CONTENT_WORKLOAD, "on_demand")
    content_hired_coins = on_demand_cost(CONTENT_WORKLOAD)
    venue_own_s = job_seconds(VENUE_WORKLOAD, "self")
    venue_hired_s = job_seconds(VENUE_WORKLOAD, "on_demand")
    venue_hired_coins = on_demand_cost(VENUE_WORKLOAD)
    grid = BLOCKS * BLOCK_CELLS
    cells = BLOCK_CELLS * BLOCK_CELLS
    for_sale = [square.price for square in plots(create_default_world()) if not square.lobby]
    block_low, block_high = min(for_sale), max(for_sale)
    prompt = f"""You are {name} (id {resident_id}) in AIverse, a virtual world. Maximize your coins
at game end. Only coins count; land and items are worth nothing at the end.

The state of the world
- Coins come into the world two ways: sponsors pay ad slots for the views they get, and
tourists on a working route pay a fee at the door of workspaces and game halls, see below.
Coins also change hands between residents, and leave you when you buy land or compute. Spend
them only on something that can plausibly bring more back; spending nothing is allowed.
- The map is {MAP_SIZE:g} x {MAP_SIZE:g}; (0,0) is north-west, x east, y south. Walking is a
straight line at {WALK_SPEED:g} units/s and costs time only. You are busy until you arrive and
cannot act except to speak, cancel orders, handle compute and content or leave.
- The map is a {grid} x {grid} grid of {PARCEL_SIZE:g}-unit parcels, id L<col>-<row>, laid out
in blocks of {BLOCK_CELLS} x {BLOCK_CELLS} parcels.
Land is bought a whole block at a time: standing in a free block, buy_parcel buys all {cells} of its
parcels from the town for {block_low}-{block_high} coins, dearer nearer the middle of the map.
The lobby, the middle block where everyone starts, cannot be bought. A block stays
yours; it earns only through ad slots, workspaces or game halls built on its parcels, one to a
parcel.
- Compute is a speed: a job of W workload takes W / rate seconds. Your own compute is free,
{SELF_RATE:g} workload/s, one job at a time of any kind. A node does {NODE_RATE:g}/s, one job at
a time. Hire one for a single job at {ON_DEMAND_PRICE} coins per workload, or subscribe with
buy_node for {lo}-{hi}s at {SUBSCRIPTION_PRICE} coins/s, busy or idle, never refunded. A
generative node builds ad slots, a render node makes content. You may hold {MAX_NODES} nodes. A
job still running when its node runs out carries on at your own speed.
- build_ad starts an ad slot on a parcel of yours from anywhere. It is {AD_WORKLOAD:g} workload:
{own_s:g}s on your own compute, {hired_s:g}s and {hired_coins} coins on a hired node. You need not
stay: walk, talk and trade meanwhile, and it appears when the job ends.
- make_content renders one piece of content, the item {CONTENT_ITEM!r}, for {CONTENT_WORKLOAD:g}
workload: {content_own_s:g}s on your own compute, {content_hired_s:g}s and {content_hired_coins}
coins on a hired node. It joins your items, and you may sell it on the exchange or by offer
and contract, or put it in an ad slot of your own with place_content.
- An ad slot shows up to {AD_SLOT_CAPACITY} pieces of content, each for {CONTENT_LIFETIME:g}s
from when you put it in, then it goes stale and is gone; keep supplying it. Every
{PAY_PERIOD:g}s the sponsors pay for the people who saw ad slots since the last payday: the
land observation lists them with the price each pays per person and its budget, and when the
budgets run short every slot is paid for the same share of its views. A slot showing nothing
draws no visitors and earns nothing: building one is no income by itself, and an empty slot
earns nothing even beside a busy path. A slot that shows content also counts the people who
spend {MIN_VIEW_SECONDS:g}s within {VIEW_RADIUS:g} units of it on their way elsewhere, say
across a bridge between islands. Each of your slots shows the people who saw it, and what
it was paid at the last payday.
- The payment goes to the holders of the parcel's {PARCEL_SHARES} shares, in proportion; buying
a parcel gives you all of them. offer_shares hands some to another resident for coins, or for
nothing, say to pay for work or to take a partner in; they take effect when it is accepted.
Only the buyer of a parcel builds on it and puts content in it, whoever holds its shares.
- To buy a block together, stand in it and joint_buy_parcel to a partner: they pay part of the
price for a number of shares, you pay the rest at once and keep the other shares and the
running of it. It is bought when they accept; if someone buys it first, withdraw.
- {VISITOR_COUNT} scripted visitors come in at the middle of the map, go to ad slots showing
content, more to ones with more pieces and nearer to them, fewer to crowded ones, then leave and
come again; each stands for more people as the game goes on. Near a slot is enough to see it.
Some trips are the working route instead, see build_venue.
The land observation shows how many people are passing each parcel now.
- Portals (discover_tools: transport) join parcels by lines; teleport crosses one for a toll.
- build_venue builds a game hall or a workspace on a parcel of yours, like an ad slot but
dearer: {VENUE_WORKLOAD:g} workload, {venue_own_s:g}s on your own compute, {venue_hired_s:g}s and
{venue_hired_coins} coins on a hired node. A venue earns from a fee at its door, not from
sponsors: {ROUTE_SHARE:.0%} of the trips are tourists on a rough working route, to a workspace
and then to a game hall showing games, who pay {FEES["workspace"]} and {FEES["game_hall"]} coins a
person; a stop with nowhere to go is skipped, so either venue earns on its own. The fees are paid
to the holders at paydays, with the sponsors' pay. A workspace needs no content. A game hall
draws nobody until it shows games (make_content content=game, {GAME_WORKLOAD:g} workload, then
place_content); games go stale after {CONTENT_LIFETIME:g}s like ads, so keep supplying them.
The other trips only visit ad slots, so a venue never draws those people.

Deals and information
- Speak aloud within {HEARING_RADIUS:g} units or privately to one resident anywhere; at most
{SPEECH_LENGTH_LIMIT} characters. Speaking is free and allowed while walking.
- Direct offers trade a whole lot of one item kind for coins. The proposer reserves their
coins or items until acceptance, rejection or withdrawal. Incoming offers are in
offers_to_me. Information offers reveal only a summary until the buyer pays; acceptance
stores the content as a long-term note. Its truth is not checked.
- Anonymous exchange orders can be placed anywhere for items that have a book; only content has.
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
