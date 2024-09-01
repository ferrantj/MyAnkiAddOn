import copy
import datetime
from dataclasses import dataclass, asdict
from typing import Sequence, TypeVar, Callable, List, Optional
from aqt import mw, gui_hooks
from aqt.utils import showInfo, askUser
from math import floor
from anki.decks import DeckConfigDict


@dataclass
class TimeLimitConfig:
    deck_name: str
    review_time_limit_sec: float = 3600  # 1 hour
    new_card_assumed_repeat: int = 5


@dataclass
class RetirementConfig:
    deck_name: str
    max_interval_days: float


@dataclass
class MonolithConfig:
    review_time_limits: List[TimeLimitConfig]
    deck_retirements: List[RetirementConfig]
    verbose: bool = True
    dryrun: bool = True
    debug: bool = False
    retired_tag: str = "Retired"
    suspend_frac: float = 0
    unsuspend_buffer_frac: float = 1
    min_count: int = 0
    goal_retention: float = 0.8
    days_to_avg: int = 30
    default_review_seconds: int = 60
    last_run: float = 0

    @classmethod
    def from_dict(cls, data: Optional[dict]):
        data = copy.deepcopy(data or {})
        data["review_time_limits"] = [
            TimeLimitConfig(**x) for x in data.get("review_time_limits", [])
        ]
        data["deck_retirements"] = [
            RetirementConfig(**x) for x in data.get("deck_retirements", [])
        ]
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self) -> None:
        data = self.to_dict()
        mw.addonManager.writeConfig(__name__, data)


# Get config
config = MonolithConfig.from_dict(mw.addonManager.getConfig(__name__))

T = TypeVar("T")


def display_errors(fn: Callable[..., T]) -> Callable[..., T]:
    if not config.debug:
        return fn

    def decorated_fn(*args, **kwargs) -> T:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            showInfo("Exception: " + str(e))

    return decorated_fn


def dryrun_fence(fn: Callable[..., None]) -> Callable[..., None]:
    def nop(*args, **kwargs) -> None:
        return None

    if config.dryrun:
        return nop
    return fn


@dryrun_fence
def suspend_cards(cards: Sequence[int]) -> None:
    mw.col.sched.suspend_cards(cards)


@dryrun_fence
def unsuspend_cards(cards: Sequence[int]) -> None:
    mw.col.sched.unsuspend_cards(cards)


@dryrun_fence
def tag_cards(cards: Sequence[int], tag) -> None:
    notes = set()
    for card in cards:
        note = mw.col.get_card(card).note()
        if note in notes:
            continue
        note.add_tag(tag)
        notes.add(note)
    mw.col.update_notes(list(notes))


@dryrun_fence
def save_config(deck_config: DeckConfigDict) -> None:
    mw.col.decks.save(deck_config)


def get_day() -> float:
    return datetime.datetime.combine(
        datetime.datetime.today().date(), datetime.datetime.min.time()
    ).timestamp()


@display_errors
def suspendLeeches() -> None:
    if config.last_run >= get_day():
        return

    review_card_inds = mw.col.find_cards(f"is:review and (-tag:{config.retired_tag} or -is:suspended)")
    suspended_cards = set(
        mw.col.find_cards(f"is:review and is:suspended")
    )

    review_card_times = [
        mw.col.db.scalar("select sum(time)/1000.0 from revlog where cid = ?", ind)
        for ind in review_card_inds
    ]
    review_card_times, review_card_inds = zip(
        *sorted(zip(review_card_times, review_card_inds))
    )

    suspend_ind = floor(len(review_card_inds) * (1.0 - config.suspend_frac))
    suspend_ind = max(suspend_ind, config.min_count)  # at least min_count cards
    suspend_ind = max(min(suspend_ind, len(review_card_inds) - 1), 0)  # within bounds
    suspend_time = review_card_times[suspend_ind]

    unsuspend_frac = max(0.0, config.suspend_frac + config.unsuspend_buffer_frac)
    unsuspend_ind = floor(len(review_card_inds) * (1.0 - unsuspend_frac))
    unsuspend_ind = max(unsuspend_ind, config.min_count)  # at least min_count cards
    unsuspend_ind = max(min(unsuspend_ind, len(review_card_inds) - 1), 0)  # within bounds
    unsuspend_time = review_card_times[unsuspend_ind]

    assert unsuspend_ind <= suspend_ind

    cards_to_suspend = set(review_card_inds[suspend_ind + 1 :]) - suspended_cards
    cards_to_unsuspend = set(review_card_inds[:unsuspend_ind]) & suspended_cards

    if not config.verbose or askUser(
        f"total_review_cards: {len(review_card_inds)}\n"
        f"suspend_time: {suspend_time / 60:.2f} minutes\n"
        f"unsuspend_time: {unsuspend_time / 60:.2f} minutes\n"
        f"suspend_cards: {len(cards_to_suspend)}\n"
        f"unsuspend_cards: {len(cards_to_unsuspend)}\n\n"
        "Continue?"
    ):
        suspend_cards(cards_to_suspend)
        unsuspend_cards(cards_to_unsuspend)


@display_errors
def retire() -> None:
    if config.last_run >= get_day():
        return
    for retire_config in config.deck_retirements:
        deck_card_inds = mw.col.find_cards(
            f'"deck:{retire_config.deck_name}" and is:review and (-is:suspended or -tag:{config.retired_tag})'
        )

        deck_times = [mw.col.card_stats_data(ind).interval for ind in deck_card_inds]
        to_suspend = [
            ind
            for t, ind in zip(deck_times, deck_card_inds)
            if t > retire_config.max_interval_days
        ]

        if not config.verbose or askUser(
            f"deck: {retire_config.deck_name}\n"
            f"deck_cards: {len(deck_card_inds)}\n"
            f"to_retire: {len(to_suspend)}\n\n"
            "Continue?"
        ):
            suspend_cards(to_suspend)
            tag_cards(to_suspend, config.retired_tag)

        # Unsuspend cards that are part of retired not but not > max days
        deck_card_inds = mw.col.find_cards(
            f'"deck:{retire_config.deck_name}" and is:review and is:suspended and tag:{config.retired_tag}'
        )

        deck_times = [mw.col.card_stats_data(ind).interval for ind in deck_card_inds]
        to_unsuspend = [
            ind
            for t, ind in zip(deck_times, deck_card_inds)
            if t <= retire_config.max_interval_days
        ]
        if not config.verbose or askUser(
            f"deck: {retire_config.deck_name}\n"
            f"retired_and_suspended_deck_cards: {len(deck_card_inds)}\n"
            f"to_unsuspend: {len(to_unsuspend)}\n\n"
            "Continue?"
        ):
            unsuspend_cards(to_unsuspend)


@display_errors
def adjustReview() -> None:
    if config.last_run >= get_day():
        return

    start_mili = int(
        (
            datetime.datetime.today() - datetime.timedelta(days=config.days_to_avg)
        ).timestamp()
        * 1000
    )
    for time_limit_config in config.review_time_limits:
        deck = mw.col.decks.by_name(time_limit_config.deck_name)

        avg_review = (
            mw.col.db.scalar(
                "select avg(revlog.time)/1000.0 from revlog "
                "left join cards on revlog.cid = cards.id "
                "left join decks on cards.did = decks.id "
                "where revlog.id > ? and decks.name like ?",
                start_mili,
                time_limit_config.deck_name + "%",
            )
            or config.default_review_seconds
        )

        avg_review_retention = 1 - (
            mw.col.db.scalar(
                "select avg(revlog.ease = 1) from revlog "
                "left join cards on revlog.cid = cards.id "
                "left join decks on cards.did = decks.id "
                "where revlog.id > ? and decks.name like ?",
                start_mili,
                time_limit_config.deck_name + "%",
            )
            or 1 - config.goal_retention
        )

        deck_config = mw.col.decks.get_config(deck["conf"])
        deck_config["rev"]["perDay"] = int(
            time_limit_config.review_time_limit_sec * avg_review_retention / avg_review
        )
        deck_config["new"]["perDay"] = int(
            time_limit_config.review_time_limit_sec
            * avg_review_retention
            / (time_limit_config.new_card_assumed_repeat * avg_review)
        )

        if not config.verbose or askUser(
            f"deck: {time_limit_config.deck_name}\n"
            f"config_new_per_day: {deck_config['new']['perDay']}\n"
            f"config_review_per_day: {deck_config['rev']['perDay']}\n\n"
            "Continue?"
        ):
            save_config(deck_config)


@dryrun_fence
def update_last_run() -> None:
    current_day = get_day()
    if config.last_run >= current_day:
        return
    config.last_run = current_day
    config.save()


gui_hooks.main_window_did_init.append(retire)
gui_hooks.main_window_did_init.append(suspendLeeches)
gui_hooks.main_window_did_init.append(adjustReview)
gui_hooks.main_window_did_init.append(update_last_run)
