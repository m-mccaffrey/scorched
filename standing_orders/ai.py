"""Computer commanders.

The bots play by the same rules as everyone else, fog of war included: they
plan from what their own units can actually see, not from the server's full
picture. A bot that cheats is a bad opponent -- it makes scouting pointless and
teaches the family the wrong lessons about the game.

Difficulty is expressed as *restraint and discipline*, not as extra
information: a Novice dribbles units forward one at a time and ignores what it
is fighting, while a Veteran masses an army, counter-picks its production, and
comes home when its base is threatened.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .fog import VisionCache, team_vision
from .grid import MODE_HOLDOUT, NEIGHBOURS, chebyshev, find_path, manhattan
from .resolve import PACT_BINDING, PLAN_BUDGET
from .units import (AIRSTRIKE_COST, AIRSTRIKE_RADIUS, ARMY_CAP_BASE, BUILDING,
                    DEPOT_CAP, HARVEST_RADIUS, UNIT, available_research,
                    cost_of_building, max_rank, promotion_cost)

@dataclass(frozen=True)
class Skill:
    """How competent a bot is, across every part of the game.

    Difficulty used to vary only in how a bot fought. Once the economy
    arrived, that stopped meaning anything: every level ran the same build,
    so outcomes converged on a coin flip and a Veteran beat a Novice barely
    more than half the time. Skill now covers economy as well, which is where
    RTS matches are actually decided.
    """
    mass_at: int          # fighters gathered before committing to an attack
    counter_pick: float   # chance of answering what the enemy actually fields
    defends: bool         # comes home when the base is threatened
    workers: int          # Engineers it will put to work
    researches: bool      # spends surplus on upgrades
    #: The most production lines it will ever run. Not the number it builds:
    #: what it wants is worked out from the army cap it has actually reached
    #: (see ``_lines_wanted``), because a Barracks is throughput and throughput
    #: is what a big army needs. A flat number here meant a bot with a cap of
    #: 36 still trickling reinforcements out of one door.
    barracks: int
    expands: bool         # builds forward depots to grow its cap
    #: Whether it negotiates at all. A Novice fights everyone until it dies,
    #: which is a perfectly good beginners' opponent; everyone above it will
    #: sue for peace when losing and gang up on whoever is running away with
    #: the war.
    talks: bool

    #: How much of the support game it plays. 0 none at all; 1 raises a Field
    #: Hospital, pulls its wounded back to it and promotes veterans; 2 also
    #: runs an Airfield and calls strikes. Graded rather than a flag because
    #: these are the most expensive things in the game, and a bot that buys
    #: them badly is worse off than one that never buys them.
    supports: int


#: Fields in order: mass_at, counter_pick, defends, workers, researches,
#: barracks, expands, talks, supports. Spelled out because the barracks and
#: workers columns sit next to each other and are easy to transpose.
SKILLS = {
    "novice": Skill(2, 0.0, False, 1, False, 1, False, False, 0),
    "moderate": Skill(4, 0.4, True, 3, True, 4, True, True, 1),
    "veteran": Skill(6, 0.85, True, 4, True, 6, True, True, 2),
    "cyborg": Skill(7, 1.0, True, 5, True, 8, True, True, 2),
}
SKILL_ORDER = ("novice", "moderate", "veteran", "cyborg")

#: Keep this much supply spare before committing to a Barracks, so a bot does
#: not bankrupt itself into having no army at all.
BARRACKS_BUFFER = 2

#: Surplus that makes a bot want another production line. One Barracks tops
#: out at roughly a unit a turn, which is exactly the rate an army bleeds at
#: the front -- so a single line is a permanent stalemate however good the
#: economy behind it.
EXPAND_SURPLUS = 26

#: Army cap per production line a bot aims for. A Barracks turns out a unit
#: every couple of turns, and an army of thirty in contact loses several a
#: turn, so one door is a queue however much money is behind it. Measured: a
#: Moderate bot on a hundred-turn match finished with two Barracks, an army of
#: twenty and 280 supply banked, against a human with twenty Barracks who
#: replaced losses as fast as they happened. Money was never the constraint.
CAP_PER_LINE = 10

#: Builders a bot keeps off the resource nodes, and the banked supply that buys
#: each one after the first. An Engineer parked on a node earns and never
#: builds again, so with one builder the whole construction programme runs at
#: one structure every four or five turns whatever the treasury looks like.
#: This is the knob that turns banked supply into buildings.
BUILDERS_BASE = 1
SUPPLY_PER_BUILDER = 45
BUILDERS_MAX = 4

#: Sentry Towers a bot will raise at home, once it has an army to lose. Eight
#: supply for something that shoots three tiles and never retreats is the best
#: trade in the game and bots were not making it at all -- they met a walled
#: base with towers at the gates using nothing but infantry.
TOWERS_AT_HOME = 3
TOWER_BUFFER = 16

#: Towers a bot raises in a Holdout, where masonry is the whole plan rather
#: than a luxury bought once the army is paid for.
TOWERS_IN_HOLDOUT = 10

#: How far from its Command Post a Holdout bot will go to found a forward
#: depot. Short, because the ground beyond the wall is where the waves are.
HOLDOUT_REACH = 8

#: One tower in this many is a Longbow. A line of nothing but Sentries is
#: free food for a Mortar Team, which outranges them; a line of nothing but
#: Longbows costs twice as much and still cannot stop anything that closes.
LONGBOW_IN = 3

#: Only start a research project with this much supply to spare, so teching
#: never comes at the price of an army.
RESEARCH_BUFFER = 20

DEFEND_RADIUS = 7

#: Supply at which a bot holds an Engineer back from harvesting to build with.
#: Roughly a Depot and change: below this there is nothing to build anyway.
BUILD_RESERVE = 12

#: Supply at which a bot will pull even its last harvesting Engineer off a
#: node to build with. A treasury this size is not short of income.
DEADLOCK_SUPPLY = 60

#: How much stronger somebody has to be before a bot will ask them for terms,
#: and how much weaker before it considers tearing a pact up. Asymmetric on
#: purpose: quick to sue for peace, slow to betray. A bot that knifes its ally
#: the moment it is marginally ahead makes the whole institution worthless,
#: and nobody signs anything with it twice.
SUE_FOR_PEACE = 1.5
BETRAY_MARGIN = 1.6

#: A bot will not break a pact before this turn. Early alliances need room to
#: mean something, and a first-turn betrayal just reads as a bug.
BETRAYAL_EARLIEST = 30

#: A war nobody has had time to fight yet cannot be ended by agreement. Bots
#: call for an armistice when the shooting has stopped, and once they had real
#: armies to compare, three of four read themselves as the underdog, truced
#: their way round the table and ended the war on turn 23 with nothing decided.
#: A human who sat down for a war is owed one. Peace becomes available later;
#: the betrayal rules can restart a war that went quiet before then, which is a
#: better shape for a match anyway -- a pause, then somebody breaks it.
PEACE_EARLIEST = 45

#: Turns without taking any more ground before a bot calls a war stuck.
#:
#: Weariness first read "am I behind?", which handed won wars away: a bot three
#: times its enemy's size proposed terms on turn 110 and the loser accepted
#: gratefully. Gating it on being behind fixed that and broke the other end --
#: a leader who could not finish the job never asked for anything, and
#: four-player matches ran past 250 turns. Neither question is the right one.
#: The right one is whether the war is *going* anywhere, which is a question
#: about progress, not about size.
STALE_TURNS = 40

#: A war this old is going nowhere, and a bot will start offering terms to
#: anyone it is still fighting.
#:
#: Without this, a web of half-signed truces can reach a state that can be
#: neither won nor ended: in a 2v2 where both pairs partly truced across the
#: line, the war reduced to two commanders grinding a third they could not
#: finish, nobody was losing badly enough to sue, and the one commander who
#: could have been offered terms was a Novice -- who accepts anything but
#: never asks. Five matches in six ran to the turn limit. Wars end.
WAR_WEARY = 110

#: Nor will it talk at all before this turn. Early on everyone owns four units
#: and a Command Post, so "who is winning" is one unlucky skirmish of noise --
#: and bots read that noise as catastrophe and sued for peace on turn eight.
DIPLOMACY_EARLIEST = 15

#: Supply a bot keeps back before buying into the support game at all. These
#: are luxuries: an Airfield bought instead of an army loses the match before
#: it ever gets to fly.
SUPPORT_BUFFER = 40

#: Promotions a bot will buy in one turn, and the surplus it insists on
#: keeping while it does. Both exist to stop ranks crowding out the army.
PROMOTIONS_PER_TURN = 1
PROMOTE_BUFFER = 60

#: A unit this far below full health is worth walking back to a hospital.
#: Higher than it looks on purpose -- a unit that trudges home over a scratch
#: spends more turns off the line than the health is worth.
WOUNDED_SHARE = 0.5

#: Enemies that have to be within one blast for a strike to be worth 30
#: supply. Two is about break-even against what it costs to replace them.
STRIKE_WORTH = 2

#: Supply held back from a strike. Far shallower than the reserve the other
#: luxuries keep, because a strike is the only one that pays off this turn.
STRIKE_RESERVE = 10

#: How much stronger than the visible enemy force a bot wants to be before it
#: stops trading in the middle and marches on a Command Post.
PRESS_ADVANTAGE = 1.35

#: Fighters that make a bot commit to an attack regardless of the odds.
#:
#: The other trigger for committing is being at the army cap -- at the ceiling,
#: waiting buys nothing -- and that quietly stopped working the moment bots
#: learned to keep building Depots: a bot whose cap rises every few turns is
#: never *at* it, so it sat at home out-expanding an enemy it never attacked.
#: Matches that failed to resolve inside 140 turns went from 30% to 52% on that
#: alone.
#:
#: Flat, and deliberately not a multiple of ``mass_at``. Scaling it by the
#: group size a tier likes to gather inverted the whole ladder: Veteran waits
#: for six where Moderate waits for four, so tying commitment to it made the
#: more cautious tier commit *later* as well as in bigger blocks, and Moderate
#: beat both tiers above it 11 games in 12. mass_at is about cohesion. This is
#: about when an army is a war-winning force, which does not depend on taste.
COMMIT_ARMY = 14

#: Fighters this close to the spearhead count as "with the army". Without a
#: rally step the bot feeds reinforcements to the front one at a time, where
#: they lose every fight three-to-one; a tighter army cap used to hide this by
#: keeping everyone bunched together.
RALLY_RADIUS = 5

#: How close to the objective, or to a visible enemy, before a bot stops
#: marching and starts advancing ready to fight.
#:
#: Advancing the whole way is a serious mistake in this game: reinforcements
#: appear at home, so anything that slows an attacker across the map hands the
#: advantage to the defender. Bots that advanced from their own doorstep
#: stopped resolving matches at all.
CONTACT_DISTANCE = 5

#: A threat must be at least this costly, and this large a share of our own
#: army, before it is worth pulling troops off an attack.
MIN_THREAT = 8
THREAT_SHARE = 0.30


class BotBrain:
    def __init__(self, skill: str = "moderate",
                 rng: random.Random | None = None) -> None:
        self.skill = skill if skill in SKILLS else "moderate"
        self.rng = rng or random.Random()
        #: The most ground this commander has ever held, and when it last grew.
        #: The only memory a brain keeps between turns, and it is what tells a
        #: stalled war from a winnable one.
        self._ground_high = -1
        self._ground_since = 0

    # -- top level ---------------------------------------------------------
    def plan(self, match, me) -> list:
        state = match.state
        cache = VisionCache(state.map)
        vision = team_vision(state, me.team, cache)

        my_units = state.units_of(me.pid)
        my_buildings = [b for b in state.buildings_of(me.pid)]
        enemies = [u for u in state.units.values()
                   if u.alive and not state.allied(u.owner, me.pid)
                   and u.tile in vision]
        enemy_buildings = [b for b in state.buildings.values()
                           if b.alive and not state.allied(b.owner, me.pid)
                           and b.tile in vision]

        orders: list = []
        orders += self._diplomacy(match, me)
        # Put Engineers on nodes they can already work *before* spending them
        # on construction. Doing it the other way round sends the whole labour
        # force off to build a distant depot while a node beside the Command
        # Post sits idle.
        worker_orders, busy, shovels = self._workers(match, me, my_units)
        orders += worker_orders
        orders += self._economy(match, me, my_buildings, my_units, enemies,
                                busy, shovels)
        orders += self._army(match, me, my_units, my_buildings, enemies,
                             enemy_buildings, vision)
        return orders

    # -- diplomacy ---------------------------------------------------------
    def _might_of(self, state, pid: int) -> float:
        """A rough public reckoning of how a commander is doing.

        Units and structures, which anybody watching the standings can count.
        Bots respect fog everywhere else, and this is the one thing that is
        genuinely common knowledge at a table: who is winning.
        """
        units = sum(UNIT[u.code].cost for u in state.units_of(pid)
                    if u.code in UNIT)
        works = sum(1 for b in state.buildings_of(pid) if b.operational)
        return units + works * 4 + 1.0

    def _diplomacy(self, match, me) -> list:
        """Sue for peace when losing, gang up on whoever is winning.

        This is the only brake the game has on a runaway leader: in a four-way
        war, what stops the strongest commander simply staying strongest is the
        other three noticing.

        Getting it wrong is easy in both directions. Too eager and everyone
        signs with everyone by turn thirty and the war fizzles into a hundred
        turns of nobody shooting anybody. Too reluctant and it never fires at
        all. The rules below were written against both failures.
        """
        state = match.state
        orders: list = []
        # Nobody negotiates with the Swarm, and the commanders are already on
        # the same side. Left switched on, a bot losing a bad wave read the
        # Swarm as a rival running away with the war and sued it for peace.
        if state.mode == MODE_HOLDOUT:
            return orders
        mine = self._might_of(state, me.pid)
        others = [p for p in state.players.values()
                  if p.alive and p.pid != me.pid]

        # Before anything else, and before the early return: how long has this
        # war been static? Ground held is the game's own measure of who is
        # getting anywhere, so a commander whose best-ever holding has not
        # budged in STALE_TURNS turns is in a war that is going nowhere,
        # whatever the relative army sizes say.
        ground = state.holding(me.pid)
        if ground > self._ground_high:
            self._ground_high = ground
            self._ground_since = state.turn
        stalled = state.turn - self._ground_since >= STALE_TURNS

        if not others:
            return []
        if state.turn < DIPLOMACY_EARLIEST:
            return []
        # A truce with your only remaining enemy is not diplomacy, it is
        # quitting: nothing else can happen on the board afterwards, and the
        # match ends in an armistice with one side clearly ahead. Half of all
        # two-player matches ended that way, several on the exact turn peace
        # became legal. Diplomacy needs a third party to be about anything.
        #
        # Counted in players and not in blocs on purpose. A 2v2 is two sides,
        # but a truce there still leaves the other enemy shooting, so the
        # institution keeps working -- and counting blocs disarmed the brake
        # that stops a teams match grinding to the turn limit.
        two_sided = sum(1 for p in state.players.values() if p.alive) <= 2
        strongest = max(others, key=lambda p: self._might_of(state, p.pid))
        at_war = [p for p in others if state.hostile(me.pid, p.pid)]
        burden = sum(self._might_of(state, p.pid) for p in at_war)
        weakest = min([mine] + [self._might_of(state, p.pid) for p in others])
        best_rival_ground = max(
            (state.bloc_holding(p.pid) for p in others
             if state.bloc_of(p.pid) != state.bloc_of(me.pid)), default=0)

        # Ending a war is a thing people do, out loud, round a table, so a bot
        # never opens that conversation while there is still a war on. It will
        # join one, and it will call for the obvious: when nobody alive is
        # shooting at anybody, the war has already stopped and somebody should
        # say so. Without that last clause bot-only matches truced themselves
        # into a frozen stalemate and ran to the turn limit six times in eight.
        if me.pid not in state.armistice and state.turn >= PEACE_EARLIEST:
            ours = state.bloc_holding(me.pid)
            joining = state.armistice and (ours >= best_rival_ground
                                           or mine * SUE_FOR_PEACE < burden)
            if joining or state.everyone_at_peace():
                orders.append({"o": "armistice"})

        for other in sorted(others, key=lambda p: p.pid):
            theirs = self._might_of(state, other.pid)
            pact = state.pact_between(me.pid, other.pid)
            offered = state.offers.get((other.pid, me.pid))

            # ACCEPTING is not gated on skill. A Novice takes any deal put in
            # front of it -- which makes it a gentle opponent, and matters more
            # than it sounds: while one commander refused to talk at all, peace
            # was unreachable for everybody and two bots who had stopped
            # fighting each other sat in a stalemate for a hundred turns.
            if offered is not None and not two_sided:
                if not SKILLS[self.skill].talks:
                    orders.append({"o": "accept", "from": other.pid})
                    continue
                # Take terms from somebody clearly beating you, or when you
                # are the weakest left and need the war to get smaller.
                if theirs > mine * SUE_FOR_PEACE or mine <= weakest:
                    orders.append({"o": "accept", "from": other.pid})
                    continue

            if not SKILLS[self.skill].talks:
                continue

            if pact == "war" and not two_sided:
                # Ask for terms when genuinely losing to this one, or when the
                # front-runner is beating us and this is a sideshow we cannot
                # afford. Note the second requires actually being at war with
                # the leader -- without that clause every bot sued everybody on
                # turn one and no war ever started.
                losing = theirs > mine * SUE_FOR_PEACE
                # Weariness is for wars that are going nowhere, and a war you
                # are winning is going somewhere. Ungated, this handed the
                # match away from in front: a bot three times its enemy's size
                # and marching on their Command Post proposed terms on turn
                # 110, the loser accepted gratefully, and a won war was
                # recorded as a draw. Half of all unresolved duels were this.
                weary = (state.turn >= WAR_WEARY
                         and (stalled or mine <= theirs * SUE_FOR_PEACE))
                # ...and not with somebody we are beating. The clause below
                # used to be "the leader is ahead of me", full stop, which in a
                # four-way war is true for almost everybody: three bots would
                # truce with each other over a leader none of them then fought,
                # the board went quiet, and the match ended in an armistice
                # before turn 25. You do not buy off an enemy weaker than you.
                sideshow = (other.pid != strongest.pid
                            and theirs * SUE_FOR_PEACE >= mine
                            and state.hostile(me.pid, strongest.pid)
                            and self._might_of(state, strongest.pid)
                            > mine * SUE_FOR_PEACE)
                if ((losing or sideshow or weary)
                        and (me.pid, other.pid) not in state.offers):
                    orders.append({"o": "propose", "to": other.pid,
                                   "pact": "truce"})

            elif (state.turn >= BETRAYAL_EARLIEST
                  and state.turn - state.pact_since.get(
                      state.pair(me.pid, other.pid), state.turn)
                  >= PACT_BINDING):
                # A war has to be winnable or it is not a war. Take on a new
                # enemy only when we could carry the wars we would then have --
                # so two equals grinding a third do not turn on each other, but
                # somebody comfortably ahead of a neighbour they are not
                # fighting eventually does.
                if mine > (burden + theirs) * BETRAY_MARGIN:
                    orders.append({"o": "declare", "to": other.pid})
        return orders

    # -- engineers ---------------------------------------------------------
    def _workers(self, match, me, my_units) -> tuple:
        """Park idle Engineers on nodes a depot can actually reach.

        Returns the orders, the set of Engineers now spoken for so the economy
        does not hand the same worker a building job as well, and the builder
        corps: Engineers deliberately kept off the nodes with a shovel in hand.

        This step used to claim every free Engineer for a node before the
        economy got a look, and since an Engineer on a node never becomes free
        again, that was the whole reason bots finished matches with one
        building, an army capped at twelve and three hundred supply they could
        not spend. Holding *one* back was the first fix and not enough: one
        shovel raises a structure every five turns, so a bot with a cap of 36
        and 150 supply banked still ran two Barracks.
        """
        state = match.state
        receivers = state.receivers_of(me.pid)
        orders: list = []
        busy: set = set()
        taken = {(u.x, u.y) for u in my_units if u.builder}
        crew = [u for u in my_units if u.builder]
        shovels = self._builder_corps(state, me, crew, receivers)
        for worker in crew:
            if worker.job is not None:
                busy.add(worker.uid)
                continue
            if worker.uid in shovels:
                continue                   # keep this one holding a shovel
            if state.map.is_node(worker.x, worker.y) and any(
                    chebyshev(worker.tile, r.tile) <= HARVEST_RADIUS
                    for r in receivers):
                busy.add(worker.uid)           # already earning; leave it be
                continue
            node = self._workable_node(state, me, worker, receivers, taken)
            if node is not None:
                taken.add(node)
                busy.add(worker.uid)
                orders.append({"o": "move", "uid": worker.uid, "to": list(node)})
        return orders, busy, shovels

    def _builder_corps(self, state, me, crew, receivers) -> set:
        """The Engineers to keep off the nodes, given what is in the treasury.

        One was the old answer and it was the ceiling on everything: a builder
        spends a couple of turns walking and two or three building, so a single
        shovel raises one structure every five turns no matter how rich the bot
        is. That is why bots banked hundreds of supply -- not because they had
        nothing worth buying, but because they had nobody free to buy it with.
        The second and third builders are bought with the surplus itself, so a
        poor bot still puts everyone to work.

        Drafted from the Engineers who are *not* earning first. Only a bot with
        real money in the bank takes one off a live node, and it never takes the
        last one: somebody has to keep the lights on.
        """
        if me.supply < BUILD_RESERVE or len(crew) < 2:
            return set()
        want = BUILDERS_BASE + (me.supply - BUILD_RESERVE) // SUPPLY_PER_BUILDER
        want = max(0, min(want, BUILDERS_MAX, len(crew) - 1))
        if not want:
            return set()

        def earning(worker) -> bool:
            return state.map.is_node(worker.x, worker.y) and any(
                chebyshev(worker.tile, r.tile) <= HARVEST_RADIUS
                for r in receivers)

        free = [u for u in crew if u.job is None]
        # Idle hands first, then whoever is standing on a node -- and the
        # earners only once the treasury says throughput matters more than
        # income. A builder that never leaves its node is not a builder, which
        # is what the old version amounted to: it held back whichever Engineer
        # came last, node or no node, and that one was almost always earning.
        draft = [u for u in free if not earning(u)]
        if len(draft) < want and me.supply >= DEADLOCK_SUPPLY:
            draft += [u for u in free if earning(u)]
        return {u.uid for u in draft[:want]}

    def _lines_wanted(self, state, me) -> int:
        """How many Barracks this bot is trying to have standing."""
        cap = state.army_cap_of(me.pid)
        return min(SKILLS[self.skill].barracks, 1 + cap // CAP_PER_LINE)

    def _workable_node(self, state, me, worker, receivers, taken):
        best = None
        for node in state.map.nodes:
            if node in taken:
                continue
            owner = state.node_owner.get(node)
            if owner is not None and not state.allied(owner, me.pid):
                continue
            if not any(chebyshev(node, r.tile) <= HARVEST_RADIUS
                       for r in receivers):
                continue
            distance = manhattan(node, worker.tile)
            if best is None or distance < best[0]:
                best = (distance, node)
        return best[1] if best else None

    # -- production --------------------------------------------------------
    def _economy(self, match, me, my_buildings, my_units, enemies,
                 busy=frozenset(), shovels=frozenset()) -> list:
        state = match.state
        busy = set(busy)
        orders: list = []
        budget = me.supply
        bases = [b for b in my_buildings if b.code == "base" and b.operational]
        barracks = [b for b in my_buildings if b.code == "barracks"]
        depots = [b for b in my_buildings if b.code == "depot"]
        workers = [u for u in my_units if u.builder]
        receivers = state.receivers_of(me.pid)
        # An Engineer standing on a live node is earning its keep; pulling it
        # off to go and build something is how a bot starves itself.
        earning = {u.uid for u in workers
                   if u.uid not in shovels
                   and state.map.is_node(u.x, u.y)
                   and any(chebyshev(u.tile, r.tile) <= HARVEST_RADIUS
                           for r in receivers)}
        idle_workers = [u for u in workers
                        if u.job is None and u.uid not in earning
                        and u.uid not in busy]
        if not bases:
            return orders

        def hands_for(price: int, pull_at: int):
            """An Engineer for a support building, idle or otherwise.

            Engineers park on resource nodes and stay there, so after the
            opening there is essentially never an idle one -- which is why
            bots measured zero Hospitals and zero Airfields built in a whole
            match while sitting on hundreds of spare supply. A rich bot can
            afford to take an Engineer off a node for two turns.

            Only the support rules use this. Letting the economy rules bid the
            same way was worse, not better: whichever rule ran first took the
            Engineer, and a bot that spent its last one on a third Barracks
            never built a Depot at all.
            """
            if idle_workers:
                return idle_workers[0]
            if budget < price + pull_at:
                return None
            free = [w for w in workers
                    if w is not None and w.job is None and w.uid not in busy]
            spare = [w for w in free if w.uid not in earning]
            if spare:
                return spare[0]
            # Normally somebody has to stay and work, or the surplus that
            # justified this dries up. The exception is a deadlock that bots
            # genuinely got stuck in: at the army cap you cannot train another
            # Engineer, with one Engineer and it harvesting you cannot build a
            # Depot, and without a Depot the cap never rises. One bot sat in
            # that with eleven hundred supply banked. When the treasury is
            # that full the harvest is the cheapest thing to give up.
            earners = [w for w in free if w.uid in earning]
            if not earners:
                return None
            stuck = (state.army_size(me.pid) >= state.army_cap_of(me.pid)
                     and budget >= DEADLOCK_SUPPLY)
            return earners[0] if len(earners) > 1 or stuck else None

        #: Sites promised this turn, so two rules cannot claim one square.
        spoken_for: set = set()
        holdout = state.mode == MODE_HOLDOUT

        def commit(worker, code: str, site) -> None:
            if worker in idle_workers:
                idle_workers.remove(worker)
            busy.add(worker.uid)
            spoken_for.add(tuple(site))
            orders.append({"o": "build", "uid": worker.uid, "code": code,
                           "to": list(site)})

        # Sentry Towers. Eight supply for something that shoots three tiles
        # and never runs is the best trade on the board, and bots were not
        # making it at all: they walked infantry into walled bases with towers
        # at the gates and wondered where the army went.
        #
        # In a Holdout this runs *first*, ahead of every other kind of
        # spending, because masonry is the plan rather than a luxury bought
        # once the army is paid for. The first attempt instead held a reserve
        # back from the recruiting queue, which was much worse than it sounds:
        # a Holdout bot's whole treasury is about fifteen supply, so a
        # twelve-supply reserve stopped it training anything at all while the
        # Depot and Barracks rules went on spending the rest -- fifteen units
        # and two towers by wave ten, and a loss on every seed. Priority is a
        # better tool than a reserve.
        towers = [b for b in my_buildings if b.code == "tower"]
        tower_target = TOWERS_IN_HOLDOUT if holdout else TOWERS_AT_HOME
        tower_buffer = TOWER_BUFFER // 4 if holdout else TOWER_BUFFER

        def raise_towers(spend: int) -> int:
            # No Barracks precondition in a Holdout: a gun at the door is the
            # first thing worth owning, not a reward for having an economy.
            #
            # A line of Sentry Towers is not a defence on its own: a Mortar
            # Team reaches one tile further than a Sentry and shells it to
            # rubble from a square it cannot be shot back from. So a share of
            # the line is Longbows, which reach one further again. Measured
            # before this: the wave-eight Mortars took the towers apart, the
            # army followed them in, and the line broke on every seed.
            longbows = sum(1 for b in towers
                           if b is not None and b.code == "longbow")
            while ((SKILLS[self.skill].defends or holdout)
                   and (barracks or holdout) and len(towers) < tower_target
                   and spend >= tower_buffer):
                # One in LONGBOW_IN, and never the first one: a Sentry at a
                # door on turn three is worth more than a Longbow on turn six.
                code = ("longbow" if towers and longbows * LONGBOW_IN < len(towers)
                        else "tower")
                price = cost_of_building(code, me.research)
                if spend < price + tower_buffer:
                    code = "tower"
                    price = cost_of_building(code, me.research)
                    if spend < price + tower_buffer:
                        break
                worker = hands_for(price, tower_buffer)
                site = self._tower_site(state, bases[0].tile, spoken_for)
                if worker is None or site is None:
                    break
                commit(worker, code, site)
                spend -= price
                towers.append(None)
                longbows += 1 if code == "longbow" else 0
            return spend

        if holdout:
            budget = raise_towers(budget)

        # 1. Barracks: the gate to the counter triangle. Gated on already
        #    having a Depot, so nobody opens with one and spends the match on
        #    one supply a turn -- but *ahead* of building any more Depots,
        #    because there is exactly one free Engineer and whichever rule
        #    asks first gets it. When the Depot rules asked first, bots
        #    finished with seventeen Depots, no Barracks at all, and an army
        #    of Scouts and Troopers: half the roster never built, all game.
        #    A bot that does not expand at all is exempt from the Depot gate.
        #    A Novice builds no Depots by definition, so the gate meant it
        #    built *nothing* for a whole match: measured at turn 100 with one
        #    Command Post, an army of twelve and 305 supply banked, having
        #    never placed a single structure. A beginners' opponent should be
        #    beatable, not inert.
        price = cost_of_building("barracks", me.research)
        gate = bool(depots) or not SKILLS[self.skill].expands
        if not barracks and gate and budget >= price + BARRACKS_BUFFER:
            worker = hands_for(price, BARRACKS_BUFFER)
            site = self._site_near(state, bases[0].tile, radius=4,
                                   avoid=spoken_for)
            if worker is not None and site is not None:
                commit(worker, "barracks", site)
                budget -= price

        # 2. A depot wherever we are working, or want to work, a node. Without
        #    one in range the Engineer standing on the node sends nothing.
        for worker in ([hands_for(cost_of_building("depot", me.research), 0)]
                       if SKILLS[self.skill].expands else []):
            if worker is None:
                break
            node = self._node_needing_depot(state, me, worker, depots + bases)
            price = cost_of_building("depot", me.research)
            if node is None or budget < price:
                continue
            site = self._site_near(state, node, avoid=spoken_for)
            if site is None:
                continue
            commit(worker, "depot", site)
            budget -= price
            depots = depots + [None]           # counts toward the cap estimate
            break

        # 1b. A depot purely for the ceiling. Node-side depots alone top a bot
        #     out around 24 army however rich it gets, which is how bots ended
        #     matches sitting on a thousand unspent supply. If we are capped,
        #     the ceiling can still rise, and there is money doing nothing,
        #     the answer is another depot -- anywhere safe will do, since this
        #     one is bought for its supply_cap and not its reach.
        #     Counting what is already standing *or* going up, because the cap
        #     only counts finished ones -- without that a bot kept queueing
        #     more every turn and finished with seventeen Depots, which is
        #     fifteen more than the ceiling can use.
        enough = -(-(state.army_ceiling - ARMY_CAP_BASE) // DEPOT_CAP)
        if (SKILLS[self.skill].expands and len(depots) < enough
                and state.army_size(me.pid) >= state.army_cap_of(me.pid)
                and state.army_cap_of(me.pid) < state.army_ceiling):
            price = cost_of_building("depot", me.research)
            if budget >= price + EXPAND_SURPLUS:
                worker = hands_for(price, EXPAND_SURPLUS)
                site = self._site_near(state, bases[0].tile, radius=4,
                                   avoid=spoken_for)
                if worker is not None and site is not None:
                    commit(worker, "depot", site)
                    budget -= price

        # 3. More production, as many lines as the army cap justifies. This
        #    used to stop at a flat number per skill -- two for a Moderate --
        #    which is a queue, not a factory: the cap rises to 36 and the
        #    reinforcements still come out of one door at one unit every other
        #    turn. Several lines a turn is fine now that there is more than one
        #    builder to raise them.
        price = cost_of_building("barracks", me.research)
        while (barracks and len(barracks) < self._lines_wanted(state, me)
               and budget >= price + EXPAND_SURPLUS):
            worker = hands_for(price, EXPAND_SURPLUS)
            site = self._site_near(state, bases[0].tile, radius=5,
                                   avoid=spoken_for)
            if worker is None or site is None:
                break
            commit(worker, "barracks", site)
            budget -= price
            barracks = barracks + [None]

        if not holdout:
            budget = raise_towers(budget)

        # 3b. Support buildings, once there is an army worth supporting. A
        #     Field Hospital first -- it pays back every turn there is a
        #     casualty -- and an Airfield only for bots that will actually fly
        #     it. Both are luxuries, so both wait behind a healthy buffer.
        tier = SKILLS[self.skill].supports
        for code, needed in (("medic", 1), ("airfield", 2)):
            # Gated on an economy, not on a Barracks. Requiring one meant
            # these never got built at all, because bots turn out to build a
            # Barracks far more rarely than they should -- a separate problem,
            # and not one a Field Hospital has any reason to wait behind.
            if tier < needed or not depots:
                continue
            if any(b.code == code for b in my_buildings):
                continue
            price = cost_of_building(code, me.research)
            # An Airfield has to be bought with its first sortie, or a bot
            # spends 14 supply and three Engineer-turns on a hangar it cannot
            # afford to use -- which measured as a straight loss: bots that
            # built one went from beating Moderate to losing to it 1-5.
            price += AIRSTRIKE_COST if code == "airfield" else 0
            if budget < price + SUPPORT_BUFFER:
                continue
            worker = hands_for(price, SUPPORT_BUFFER)
            site = self._site_near(state, bases[0].tile, radius=3,
                                   avoid=spoken_for)
            if worker is None or site is None:
                continue
            commit(worker, code, site)
            budget -= cost_of_building(code, me.research)

        # 3c. Airstrikes, before promotions and out of the same purse. These
        #     used to be planned in the army step against a second, private
        #     copy of the budget, so a bot happily promised the same supply to
        #     a strike and a promotion and had one of them thrown out.
        strike_orders, budget = self._airstrikes(state, me, my_buildings,
                                                 enemies, budget)
        orders += strike_orders

        # 3d. Promote whoever has earned it -- but only once quantity has run
        #     out. Promotions are cheap enough to be tempting every turn, and
        #     a bot that took them early spent its whole economy on ranks and
        #     never built a Barracks at all: 175 promotions in a match and no
        #     production. Buying quality is what you do when you cannot buy
        #     any more quantity.
        spare = state.army_cap_of(me.pid) - state.army_size(me.pid)
        if tier >= 1 and spare <= 2:
            veterans = sorted((u for u in my_units
                               if u.blooded and u.rank < max_rank()
                               and not u.builder),
                              key=lambda u: (-u.rank, u.uid))
            # One a turn, behind a deep buffer. Promotions are cheap enough
            # that a bot allowed to buy them freely will empty its treasury
            # into ranks the same turn it hits an early, tiny cap -- and an
            # early cap wants another Depot, not a Corporal.
            for unit in veterans[:PROMOTIONS_PER_TURN]:
                price = promotion_cost(unit.rank)
                if not price or budget < price + PROMOTE_BUFFER:
                    continue
                orders.append({"o": "promote", "uid": unit.uid})
                budget -= price

        # 4. Research, once there is money doing nothing useful.
        if (SKILLS[self.skill].researches and bases[0].project is None
                and budget >= RESEARCH_BUFFER):
            options = available_research(me.research)
            affordable = [r for r in options if r.cost <= budget - BARRACKS_BUFFER]
            if affordable:
                pick = self.rng.choice(affordable)
                orders.append({"o": "research", "bid": bases[0].bid,
                               "code": pick.code})
                budget -= pick.cost

        # 5. Recruit, respecting the cap the depots actually support.
        room = state.army_cap_of(me.pid) - state.army_size(me.pid)
        wanted = self._next_unit(state, me, barracks, workers, enemies)
        for _ in range(min(3, max(0, room))):
            if wanted is None:
                break
            unit_type = UNIT[wanted]
            if budget < unit_type.cost:
                break
            source = self._producer(my_buildings, wanted)
            if source is None:
                break
            orders.append({"o": "train", "bid": source.bid, "code": wanted})
            budget -= unit_type.cost
            workers = workers + ([None] if wanted == "worker" else [])
            wanted = self._next_unit(state, me, barracks, workers, enemies)
        return orders

    def _node_needing_depot(self, state, me, worker, receivers):
        """A node worth putting a depot beside: ours, or free, and out of range.

        Skips nodes an Engineer is already walking to. Without that check the
        rule re-picked the same node every turn -- an unbuilt depot is still an
        unserved node -- and handed it to whichever Engineer happened to be
        free. Measured in a Holdout: all four Engineers carrying the identical
        job for the identical tile, none of them ever arriving, and no other
        structure raised for the rest of the match.
        """
        claimed = self._claimed_sites(state, me)
        far = HOLDOUT_REACH if state.mode == MODE_HOLDOUT else 0
        home = next((b.tile for b in state.buildings_of(me.pid)
                     if b.code == "base" and b.alive), None)
        best = None
        for node in state.map.nodes:
            owner = state.node_owner.get(node)
            if owner is not None and not state.allied(owner, me.pid):
                continue
            if any(chebyshev(node, spot) <= 1 for spot in claimed):
                continue
            if any(r is not None and chebyshev(node, r.tile) <= HARVEST_RADIUS
                   for r in receivers):
                continue
            # A Holdout bot does not go prospecting. The waves are walking the
            # open ground, and an Engineer sent across it to found a forward
            # depot is a donation. What it earns instead is bounties.
            if far and home is not None and manhattan(node, home) > far:
                continue
            distance = manhattan(node, worker.tile)
            if best is None or distance < best[0]:
                best = (distance, node)
        return best[1] if best else None

    def _claimed_sites(self, state, me) -> set:
        """Tiles this commander's Engineers already have outstanding jobs on."""
        return {unit.job[1] for unit in state.units.values()
                if unit.alive and unit.owner == me.pid and unit.job is not None}

    def _site_near(self, state, origin, radius: int = 3, avoid=()):
        """A free, buildable tile close to somewhere that does not seal it in.

        ``avoid`` is what this bot has already promised to build on this turn.
        Without it every rule in a turn picked the same closest tile: a Depot
        and a Barracks would be ordered onto one square, both charged for, and
        whichever Engineer arrived second had its job refunded and cancelled --
        so a bot that thought it was raising three buildings raised one and
        burned two Engineers walking to a site that was already taken.

        The rest of this is about not bricking yourself up. Sites were chosen
        by closeness to the Command Post and nothing else, which lays a solid
        ring of Barracks and Depots around it -- and movement in this game is
        four-directional, so a ring is a wall. Measured on a duel at turn 150: a
        Veteran with eleven Depots, six Barracks and an army of sixty had all
        thirty-five of its fighters *entirely without a route to the enemy*,
        ordered to attack every turn, penned inside their own yard. The enemy
        Command Post sat at full health for two hundred turns. This was the
        whole of "the AI does not seem to want to win": it wanted to, and it
        had bricked itself in. Two rules follow: leave the Command Post a moat,
        and never take the tile that closes the pocket.
        """
        # A tile somebody is already walking to with a shovel is spoken for,
        # whoever they are. It does not block the build order -- the site is
        # empty until the structure goes up -- but two Engineers converging on
        # one square is two walks for one building.
        occupied = (set(state.occupancy()) | set(avoid)
                    | {u.job[1] for u in state.units.values()
                       if u.alive and u.job is not None})
        # Only structures and terrain count as walls for the escape test.
        # Occupancy includes *units*, and units move: counting them meant a
        # yard with somebody standing in each gap read as permanently sealed,
        # every candidate site looked equally hopeless, and the bot answered
        # "nowhere to build" for the rest of the match -- 311 supply banked at
        # an army cap it could have been raising. A body in a doorway is not a
        # wall.
        walls = {b.tile for b in state.buildings.values() if b.alive} | set(avoid)
        # Widening rings, because a yard fills up. A fixed radius meant a bot
        # with seven buildings around its Command Post simply stopped finding
        # anywhere to build: it banked 311 supply at the army cap with three
        # Depots and a ceiling of 60 it could have been climbing. A base needs
        # a yard, and then it needs a bigger one.
        for span in (radius, radius + 4, radius + 8):
            candidates = []
            for dy in range(-span, span + 1):
                for dx in range(-span, span + 1):
                    tile = (origin[0] + dx, origin[1] + dy)
                    if not state.map.passable(*tile) or state.map.is_node(*tile):
                        continue
                    if tile in occupied or chebyshev(tile, origin) < 2:
                        continue
                    candidates.append(tile)
            candidates.sort(key=lambda t: (chebyshev(t, origin), t))
            for tile in candidates:
                if self._escapes(state, origin, walls | {tile}, span):
                    return tile
        return None

    def _escapes(self, state, origin, blocked, radius: int) -> bool:
        """Can somebody standing at ``origin`` still walk out past ``radius``?

        A bounded flood fill, four-directional because that is how units move.
        It gives up once it is clear of the build zone, so the usual answer
        costs a few dozen tiles rather than a map-wide search.
        """
        reach = radius + 2
        frontier = [origin]
        seen = {origin}
        budget = 8 * reach * reach
        while frontier and len(seen) < budget:
            x, y = frontier.pop()
            if chebyshev((x, y), origin) > reach:
                return True
            for dx, dy in NEIGHBOURS:
                step = (x + dx, y + dy)
                if step in seen or step in blocked:
                    continue
                if not state.map.passable(*step):
                    continue
                seen.add(step)
                frontier.append(step)
        return False

    def _producer(self, my_buildings, code: str):
        needed = UNIT[code].built_at
        options = [b for b in my_buildings
                   if b.code == needed and b.operational and len(b.queue) < 3]
        options.sort(key=lambda b: (len(b.queue), b.bid))
        return options[0] if options else None

    def _next_unit(self, state, me, barracks, workers, enemies) -> str | None:
        """Pick the next thing to build, counter-picking when skilled enough."""
        counter_chance = SKILLS[self.skill].counter_pick
        have_barracks = any(b is not None and b.operational for b in barracks)

        # Engineers first: no economy without them, and the army cap cannot
        # grow until somebody is free to raise a depot.
        # One more Engineer than there are nodes to work, because an Engineer
        # standing on a node is *employed* -- it harvests, it cannot build, and
        # it never becomes free again. Bots measured zero spare Engineers from
        # turn 25 onward, which meant no Depots, no Barracks, an army capped at
        # twelve for the whole match and three hundred supply banked with
        # nothing able to spend it. Somebody has to be holding a shovel.
        wanted = SKILLS[self.skill].workers + 1
        if len(state.receivers_of(me.pid)) > 2:
            wanted += 1
        if len(workers) < wanted:
            return "worker"

        if have_barracks and enemies and self.rng.random() < counter_chance:
            # Answer whatever the enemy has most of.
            tally: dict[str, int] = {}
            for enemy in enemies:
                tally[enemy.code] = tally.get(enemy.code, 0) + 1
            common = max(tally, key=lambda c: (tally[c], c))
            for code, unit in UNIT.items():
                if unit.beats == common and (unit.built_at == "base" or have_barracks):
                    return code

        pool = ["trooper", "scout"]
        if have_barracks:
            # A Mortar Team is the answer to masonry and nothing else, so it
            # is worth owning where there is masonry to answer -- and worth
            # nothing at all in a Holdout, where the enemy builds no
            # structures and a Mortar is a 12-supply unit that does three
            # damage to a person.
            pool = ["trooper", "ranged", "bruiser", "scout"]
            weights = [4, 3, 2, 1]
            if state.mode != MODE_HOLDOUT:
                pool.append("siege")
                weights.append(2)
        else:
            weights = [4, 2]
        return self.rng.choices(pool, weights=weights, k=1)[0]

    # -- army --------------------------------------------------------------
    def _army(self, match, me, my_units, my_buildings, enemies,
              enemy_buildings, vision) -> list:
        state = match.state
        skill = SKILLS[self.skill]
        mass_at, defends = skill.mass_at, skill.defends
        orders = []

        bases = [b for b in my_buildings if b.code == "base"]
        home = bases[0].tile if bases else None
        if state.mode == MODE_HOLDOUT:
            return self._hold_the_line(match, me, my_units, my_buildings,
                                       enemies, home)
        fighters = [u for u in my_units if u.code not in ("scout", "worker")]
        scouts = [u for u in my_units if u.code == "scout"]

        # 0. Casualties go to the rear. A hospital nobody walks to is 10
        # supply spent on scenery, and a badly hurt unit sent back into the
        # line is a free kill for the other side. They are taken off the
        # roster entirely for the trip -- a stretcher case is not an attacker.
        wards = [b for b in my_buildings
                 if BUILDING[b.code].heal and b.operational]
        if wards and SKILLS[self.skill].supports >= 1:
            for unit in list(fighters):
                if unit.hp >= unit.max_hp * WOUNDED_SHARE:
                    continue
                ward = min(wards, key=lambda b: manhattan(unit.tile, b.tile))
                bed = self._bedside(state, ward, unit)
                if bed is None:
                    continue
                fighters.remove(unit)
                if unit.tile == bed:
                    orders.append({"o": "hold", "uid": unit.uid})
                else:
                    orders.append({"o": "move", "uid": unit.uid,
                                   "to": list(bed)})

        # 1. Home defence, but proportionate. Recalling the whole army every
        # time a lone scout wanders past the base makes two defensive bots
        # yo-yo forever and neither ever commits to a siege -- measured at 7
        # matches in 8 failing to resolve. So a token raid is ignored, and a
        # real threat pulls back only the half of the army nearest home.
        defenders: list = []
        threat_tile = None
        if defends and home is not None and fighters:
            near = [e for e in enemies if chebyshev(e.tile, home) <= DEFEND_RADIUS]
            threat_cost = sum(UNIT[e.code].cost for e in near if e.code in UNIT)
            army_cost = sum(UNIT[u.code].cost for u in fighters)
            if near and threat_cost >= max(MIN_THREAT, army_cost * THREAT_SHARE):
                threat_tile = min(near, key=lambda e: chebyshev(e.tile, home)).tile
                by_home = sorted(fighters, key=lambda u: chebyshev(u.tile, home))
                defenders = by_home[:max(1, len(by_home) // 2)]

        attackers = [u for u in fighters if u not in defenders]
        for unit in defenders:
            orders.append({"o": "attack", "uid": unit.uid, "to": list(threat_tile)})

        target = None
        if attackers:
            mine = sum(UNIT[u.code].cost for u in attackers)
            theirs = sum(UNIT[e.code].cost for e in enemies if e.code in UNIT)
            # Attack when ahead, or when the army is as big as it will ever
            # get -- at the cap, waiting buys nothing and hands the initiative
            # away, which is why human RTS players push at max supply.
            #
            # Seeing no enemy is emphatically *not* evidence of advantage.
            # Treating it as one made the bot all-in on the enemy base every
            # single turn from behind fog, so it never expanded, never
            # out-economied anybody, and simply fed its army in forever.
            spare = state.army_cap_of(me.pid) - state.army_size(me.pid)
            # Either the army has stopped growing, or it is big enough that
            # growing it further is not the point any more.
            at_cap = spare <= 2 or len(attackers) >= COMMIT_ARMY
            pressing = at_cap or (bool(enemies)
                                  and mine >= theirs * PRESS_ADVANTAGE)

            if pressing and (len(attackers) >= mass_at or at_cap):
                if pressing:
                    # Finish somebody off rather than spreading damage around.
                    # In a four-way game especially, knocking one commander out
                    # shrinks the field and gets the match moving; picking the
                    # nearest target instead leaves everyone alive forever.
                    target = self._weakest_enemy_base(match, me, enemy_buildings,
                                                      home)
                elif enemies:
                    target = min(enemies, key=lambda e: manhattan(
                        e.tile, home or e.tile)).tile
                else:
                    target = self._enemy_spawn(match, me)

        if target is not None:
            # Gather before committing. The spearhead -- whoever is closest to
            # the objective -- holds while the rest close up, and the whole
            # force moves off together once it is worth moving.
            anchor = min(attackers, key=lambda u: manhattan(u.tile, target))
            grouped = [u for u in attackers
                       if manhattan(u.tile, anchor.tile) <= RALLY_RADIUS]
            if len(grouped) >= mass_at or at_cap:
                for unit in attackers:
                    orders.append({"o": self._pace(unit, target, enemies),
                                   "uid": unit.uid, "to": list(target)})
            else:
                # Gathering happens behind your own lines: march, do not
                # advance. Rallying at cautious pace is pure lost time.
                for unit in attackers:
                    stance = "attack" if enemies else "move"
                    orders.append({"o": stance, "uid": unit.uid,
                                   "to": list(anchor.tile)})
        else:
            # Not ready to commit: spread out and take the map instead, which
            # is what actually wins the match.
            claimed = set()
            for unit in attackers:
                node = self._claim_node(state, me, unit, claimed)
                if node is not None:
                    claimed.add(node)
                    orders.append({"o": "move", "uid": unit.uid, "to": list(node)})

        for scout in scouts:
            spot = self._scout_target(match, me, scout, vision)
            if spot is not None:
                orders.append({"o": "move", "uid": scout.uid, "to": list(spot)})
        return orders

    def _tower_site(self, state, home, spoken_for):
        """Where a gun goes: beside a doorway in Holdout, at home otherwise.

        Towers built in the middle of a town shoot nothing. The doorways are
        known from the map, so a Holdout bot walls the gaps rather than
        decorating its own yard.
        """
        if state.mode != MODE_HOLDOUT:
            return self._site_near(state, home, radius=3, avoid=spoken_for)
        for post in self._doorways(state, home):
            site = self._site_near(state, post, radius=2, avoid=spoken_for)
            if site is not None:
                return site
        return self._site_near(state, home, radius=4, avoid=spoken_for)

    def _hold_the_line(self, match, me, my_units, my_buildings, enemies,
                       home) -> list:
        """Holdout: there is no enemy base, so there is nothing to march on.

        Left to the war planner a bot in Holdout is lost -- it looks for the
        weakest enemy Command Post, the Swarm has never had one, and the
        fallback picks an enemy *spawn point*, which on a co-operative map is a
        teammate's front door. Measured: four bots milling about in the middle
        of their own town for eighty turns while the waves walked past them.

        Defence here is a picket, not a deathball. Each fighter is assigned the
        doorway nearest it and told to advance on whatever is coming through --
        which spreads the army over all four gates instead of concentrating it
        at one and leaving the other three open.
        """
        state = match.state
        orders: list = []
        fighters = [u for u in my_units if not u.builder]
        if not fighters:
            return orders

        orders += self._to_hospital(state, my_buildings, fighters)

        doors = self._doorways(state, home)
        for unit in fighters:
            post = min(doors, key=lambda t: manhattan(unit.tile, t)) \
                if doors else home
            # Anything already through the door outranks the door itself.
            near = [e for e in enemies
                    if manhattan(e.tile, post) <= DEFEND_RADIUS
                    or manhattan(e.tile, unit.tile) <= DEFEND_RADIUS]
            if near:
                target = min(near, key=lambda e: (manhattan(e.tile, unit.tile),
                                                  e.uid)).tile
            elif post is not None:
                target = post
            else:
                continue
            if unit.tile == target:
                orders.append({"o": "hold", "uid": unit.uid})
            else:
                orders.append({"o": "attack", "uid": unit.uid,
                               "to": list(target)})
        return orders

    def _to_hospital(self, state, my_buildings, fighters) -> list:
        """Walk the badly hurt to a Field Hospital, and off the roster."""
        orders: list = []
        wards = [b for b in my_buildings
                 if BUILDING[b.code].heal and b.operational]
        if not wards or SKILLS[self.skill].supports < 1:
            return orders
        for unit in list(fighters):
            if unit.hp >= unit.max_hp * WOUNDED_SHARE:
                continue
            ward = min(wards, key=lambda b: manhattan(unit.tile, b.tile))
            bed = self._bedside(state, ward, unit)
            if bed is None:
                continue
            fighters.remove(unit)
            if unit.tile == bed:
                orders.append({"o": "hold", "uid": unit.uid})
            else:
                orders.append({"o": "move", "uid": unit.uid, "to": list(bed)})
        return orders

    def _doorways(self, state, home):
        """Where to stand: one tile inside the wall on the way to each gate.

        Worked out from the map rather than declared on it. The step from home
        toward a gate that is still walkable is the doorway the wave will use,
        and standing on it means meeting the wave in the gap rather than in the
        open field behind it.
        """
        if home is None or not state.map.gates:
            return []
        posts = []
        for gate in state.map.gates:
            route = find_path(state.map, home, gate, set(), limit=PLAN_BUDGET,
                              partial=True)
            if route:
                # A third of the way out: past the wall, short of the gate.
                posts.append(route[max(0, len(route) // 3)])
        return posts or [home]

    def _bedside(self, state, ward, unit):
        """A free tile inside a hospital's radius, or the one already held."""
        info = BUILDING[ward.code]
        occupied = set(state.occupancy())
        best = None
        for dy in range(-info.heal_radius, info.heal_radius + 1):
            for dx in range(-info.heal_radius, info.heal_radius + 1):
                tile = (ward.x + dx, ward.y + dy)
                if tile == unit.tile:
                    return tile                # already in a bed
                if not state.map.passable(*tile) or tile in occupied:
                    continue
                distance = manhattan(tile, unit.tile)
                if best is None or distance < best[0]:
                    best = (distance, tile)
        return best[1] if best else None

    def _airstrikes(self, state, me, my_buildings, enemies,
                    budget: int) -> tuple:
        """Spend a strike on the densest thing worth bombing.

        Scored by what is actually under the blast, friendly casualties
        subtracted -- a bot that bombs its own melee is worse than one that
        never calls a strike at all.
        """
        if SKILLS[self.skill].supports < 2 or not enemies:
            return [], budget
        fields = [b for b in my_buildings
                  if BUILDING[b.code].airstrikes and b.operational]
        orders: list = []
        aimed: set = set()
        friends = [u for u in state.units.values()
                   if u.alive and state.allied(u.owner, me.pid)]
        for field in sorted(fields, key=lambda b: b.bid):
            # A strike is the one thing here that pays off the same turn, so
            # it is funded ahead of promotions and kept behind only a shallow
            # reserve -- a hangar that never launches is pure overhead.
            if budget < AIRSTRIKE_COST + STRIKE_RESERVE:
                break
            best = None
            for candidate in {e.tile for e in enemies} - aimed:
                caught = sum(1 for e in enemies
                             if chebyshev(e.tile, candidate) <= AIRSTRIKE_RADIUS)
                friendly = sum(1 for u in friends
                               if chebyshev(u.tile, candidate) <= AIRSTRIKE_RADIUS)
                score = caught - friendly
                if score < STRIKE_WORTH:
                    continue
                if best is None or (score, candidate) > best:
                    best = (score, candidate)
            if best is None:
                break
            aimed.add(best[1])
            orders.append({"o": "airstrike", "bid": field.bid,
                           "to": list(best[1])})
            budget -= AIRSTRIKE_COST
        return orders, budget

    def _pace(self, unit, target, enemies) -> str:
        """March or advance?

        Attack orders trade pace for stopping to fight whatever you meet.
        Worth paying at the point of contact; a waste while crossing empty
        ground, where arriving late is the only real danger. Units keep
        shooting either way -- the stance only decides whether they halt.
        """
        if manhattan(unit.tile, target) <= CONTACT_DISTANCE:
            return "attack"
        if any(manhattan(unit.tile, e.tile) <= CONTACT_DISTANCE for e in enemies):
            return "attack"
        return "move"

    def _claim_node(self, state, me, unit, taken=()):
        """Nearest node we do not already own. Capture is a trip, not a post."""
        best = None
        for node in state.map.nodes:
            if node in taken:
                continue
            owner = state.node_owner.get(node)
            if owner is not None and state.allied(owner, me.pid):
                continue
            distance = manhattan(node, unit.tile)
            if best is None or distance < best[0]:
                best = (distance, node)
        return best[1] if best else None

    def _weakest_enemy_base(self, match, me, enemy_buildings, home):
        """The Command Post of whichever rival is closest to being finished."""
        state = match.state
        sieges = [b for b in enemy_buildings if b.code == "base"]
        if sieges:
            def weakness(building):
                owner = building.owner
                strength = sum(UNIT[u.code].cost for u in state.units_of(owner)
                               if u.code in UNIT)
                return (strength + building.hp,
                        manhattan(building.tile, home or building.tile))
            return min(sieges, key=weakness).tile
        return self._enemy_spawn(match, me)

    def _enemy_spawn(self, match, me):
        """Where the enemy started. Public knowledge -- it is on the map."""
        state = match.state
        others = [p for p in state.players.values()
                  if p.alive and not state.allied(p.pid, me.pid)]
        if not others:
            return None
        slots = sorted(state.map.spawns)
        for player in sorted(others, key=lambda p: p.pid):
            index = sorted(state.players).index(player.pid) + 1
            if index in state.map.spawns:
                return state.map.spawns[index]
        return state.map.spawns[slots[-1]]

    def _scout_target(self, match, me, scout, vision):
        """Send scouts at whatever we cannot currently see."""
        state = match.state
        free = [n for n in state.map.nodes
                if not state.allied(state.node_owner.get(n, -99), me.pid)
                and manhattan(n, scout.tile) <= 12]
        if free:
            return min(free, key=lambda n: manhattan(n, scout.tile))
        unseen = [n for n in state.map.nodes if n not in vision]
        if unseen:
            return min(unseen, key=lambda n: manhattan(n, scout.tile))
        spawn = self._enemy_spawn(match, me)
        if spawn is not None and spawn not in vision:
            return spawn
        for _ in range(8):
            tile = (self.rng.randrange(state.map.width),
                    self.rng.randrange(state.map.height))
            if state.map.passable(*tile) and tile not in vision:
                return tile
        return None
