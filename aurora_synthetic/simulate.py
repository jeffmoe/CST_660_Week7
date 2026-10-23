"""Simulate Aurora's source systems: subscribers, orders, payments, consent,
helpdesk tickets and the producer event stream.

Behavior is driven by the assumptions register (base scenario): daily-active
share (A-EVT-01), events per active day (A-EVT-02), lifecycle/messaging events
(A-EVT-03), contact rate (A-TRN-01), monthly churn (A-GRW-04), promo-day peak
(A-EVT-04) and ingestion lag (ADR-003 micro-batch). Each subscriber has a latent
churn propensity that shapes engagement, skips, payment failures, cancel-page
visits and support contacts, so the churn model has real signal to learn.

All personal data is fictitious: example.com emails, 555-01xx phone numbers,
TEST-NET IP addresses.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

FIRST = ["Avery", "Jordan", "Riley", "Casey", "Morgan", "Quinn", "Rowan", "Sage", "Drew", "Emerson",
         "Harper", "Jamie", "Kai", "Logan", "Parker", "Reese", "Skyler", "Taylor", "Blake", "Cameron"]
LAST = ["Alder", "Birch", "Cedar", "Dune", "Ember", "Fern", "Grove", "Heath", "Isle", "Juniper",
        "Kestrel", "Linden", "Moss", "North", "Oak", "Pike", "Reed", "Stone", "Thorn", "Vale"]
STATES = {  # state: (weight, city, zip prefix)
    "CA": (0.18, "Fresno", "937"), "TX": (0.13, "Austin", "787"), "FL": (0.10, "Tampa", "336"),
    "NY": (0.09, "Albany", "122"), "WA": (0.06, "Spokane", "992"), "IL": (0.06, "Peoria", "616"),
    "PA": (0.06, "Erie", "165"), "OH": (0.05, "Dayton", "454"), "GA": (0.05, "Macon", "312"),
    "CO": (0.05, "Boulder", "803"), "NC": (0.05, "Durham", "277"), "AZ": (0.12, "Tempe", "852"),
}
SKUS = [  # sku, product, category, health_inferable
    ("SKU-1001", "Daily Multivitamin", "general", False),
    ("SKU-1002", "Magnesium Sleep Blend", "sleep", True),
    ("SKU-1003", "Prenatal Complete", "prenatal", True),
    ("SKU-1004", "Whey Protein Vanilla", "fitness", False),
    ("SKU-1005", "Omega-3 Fish Oil", "heart", True),
    ("SKU-1006", "Vitamin D3 + K2", "general", False),
    ("SKU-1007", "Probiotic 30B", "digestive", True),
    ("SKU-1008", "Electrolyte Mix", "fitness", False),
]
PLANS = {"monthly": 30, "bimonthly": 60, "quarterly": 90}
USER_AGENTS = ["Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)", "Mozilla/5.0 (Windows NT 10.0; Win64)",
               "AuroraApp/4.2 (Android 15)", "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)"]
SESSION_TYPES = (["page_view", "product_view", "add_to_cart"], [0.65, 0.25, 0.10])


@dataclass
class Config:
    subscribers: int = 600
    days: int = 150
    seed: int = 660
    end_date: date = date(2027, 3, 10)  # exclusive; the nightly build runs on this date
    tz: str = "America/Chicago"
    dau_share: float = 0.25  # A-EVT-01
    events_per_dau: float = 40  # A-EVT-02
    lifecycle_per_day: float = 1.6  # A-EVT-03
    contact_rate: float = 0.10  # A-TRN-01
    monthly_churn: float = 0.07  # A-GRW-04
    peak_multiplier: float = 3.0  # A-EVT-04
    late_window_days: int = 3  # A-OPS-03
    anonymous_share: float = 0.08
    promo_new_subscribers: int = 40

    @classmethod
    def from_assumptions(cls, a: dict[str, dict], **overrides) -> "Config":
        return cls(
            dau_share=a["A-EVT-01"]["base"], events_per_dau=a["A-EVT-02"]["base"],
            lifecycle_per_day=a["A-EVT-03"]["base"], contact_rate=a["A-TRN-01"]["base"],
            monthly_churn=a["A-GRW-04"]["base"], peak_multiplier=a["A-EVT-04"]["base"],
            late_window_days=a["A-OPS-03"]["base"], **overrides,
        )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def start_date(self) -> date:
        return self.end_date - timedelta(days=self.days)

    @property
    def promo_day(self) -> int:
        return self.days - 3

    def local_dt(self, day: int, seconds: float) -> datetime:
        d = self.start_date + timedelta(days=int(day))
        return datetime.combine(d, time(0), self.zone) + timedelta(seconds=float(seconds))


@dataclass
class World:
    cfg: Config
    subscribers: pd.DataFrame
    orders: pd.DataFrame
    payments: pd.DataFrame
    consent: pd.DataFrame
    tickets: pd.DataFrame
    events: pd.DataFrame  # producer events as landed (ingested_at set by the loader)
    notes: dict = field(default_factory=dict)


class _Events:
    def __init__(self, cfg: Config, rng: np.random.Generator):
        self.cfg, self.rng = cfg, rng
        self.cols: dict[str, list] = {k: [] for k in (
            "event_id", "event_type", "source", "event_time", "ingested_at", "subscriber_id",
            "anonymous_id", "ip_address", "user_agent", "properties")}

    def lag(self, source: str) -> timedelta:
        # Micro-batch landing within one or two 15-minute cycles; occasional offline app events.
        minutes = self.rng.uniform(1, 25)
        if source == "app" and self.rng.random() < 0.015:
            minutes += min(self.rng.exponential(360), 36 * 60)
        return timedelta(minutes=minutes)

    def add(self, etype: str, source: str, when: datetime, sub: str | None, props: str = "{}",
            device: str | None = None) -> None:
        c = self.cols
        c["event_id"].append(str(uuid.UUID(bytes=self.rng.bytes(16), version=4)))
        c["event_type"].append(etype)
        c["source"].append(source)
        utc = when.astimezone(ZoneInfo("UTC"))
        c["event_time"].append(utc)
        c["ingested_at"].append(utc + self.lag(source))
        c["subscriber_id"].append(sub)
        web = source in ("web", "app")
        c["anonymous_id"].append(hashlib.sha256(f"{device}".encode()).hexdigest()[:24] if web else None)
        c["ip_address"].append(f"203.0.113.{self.rng.integers(1, 255)}" if web else None)
        c["user_agent"].append(USER_AGENTS[self.rng.integers(len(USER_AGENTS))] if web else None)
        c["properties"].append(props)

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.cols)
        for col in ("event_time", "ingested_at"):
            df[col] = pd.to_datetime(df[col], utc=True)
        return df


def _transcript(topic: str, name: str, email: str, phone: str, order_no: str, product: str,
                rng: np.random.Generator) -> str:
    first = name.split()[0]
    lines = {
        "shipping": [
            f"Customer: Hi, this is {name}. My order {order_no} of {product} arrived with the seal broken.",
            f"Agent: I'm sorry about that, {first}. Can you confirm the email on the account?",
            f"Customer: Sure, it's {email}.",
            f"Agent: Thanks. I've sent a replacement {product} for order {order_no} at no charge.",
            "Customer: Great, how long will it take?",
            "Agent: It ships today and usually arrives in three to five business days.",
        ],
        "billing": [
            f"Customer: Hello, I was charged twice for order {order_no}.",
            f"Agent: Let me look. Can I call you back at {phone} if we get disconnected?",
            "Customer: Yes that's fine.",
            f"Agent: I see a duplicate authorization for {product}; one will drop off within a few days.",
            "Customer: Okay, thanks for checking.",
        ],
        "product": [
            f"Customer: Is {product} safe to take with my other supplements? Order {order_no}.",
            "Agent: I can't give medical advice, but the label lists all ingredients and amounts.",
            "Agent: Many customers check with their doctor or pharmacist before combining products.",
            "Customer: Got it, I'll ask my pharmacist.",
        ],
        "cancellation": [
            f"Customer: I want to cancel. This is {name}, email {email}.",
            "Agent: I can help with that. May I ask why you're cancelling?",
            f"Customer: {product} is too expensive for me right now. My last order was {order_no}.",
            "Agent: I could pause deliveries for up to three months or offer 20% off the next two boxes.",
            "Customer: A pause would work, thanks." if rng.random() < 0.4 else "Customer: No thanks, please cancel.",
        ],
    }[topic]
    return "\n".join(lines + ["Thank you for contacting Aurora Nutrition, we are here to help."])


def simulate(cfg: Config) -> World:
    rng = np.random.default_rng(cfg.seed)
    n, days = cfg.subscribers, cfg.days
    ev = _Events(cfg, rng)

    # ------------------------------------------------------------ subscribers
    ids = [f"sub_{i:05d}" for i in range(n)]
    states = rng.choice(list(STATES), size=n, p=np.array([w for w, *_ in STATES.values()]) / sum(w for w, *_ in STATES.values()))
    plan = rng.choice(list(PLANS), size=n, p=[0.6, 0.25, 0.15])
    propensity = rng.beta(2, 6, size=n)
    existing = rng.random(n) < 0.7
    start_day = np.where(existing, rng.integers(-730, 0, n), rng.integers(0, days - 30, n))
    promo_new = rng.choice(np.flatnonzero(~existing), size=min(cfg.promo_new_subscribers, int((~existing).sum())), replace=False)
    start_day[promo_new] = cfg.promo_day

    hazard = cfg.monthly_churn / 30 * (propensity / propensity.mean()) ** 1.5
    cancel_day = np.maximum(start_day, 0) + rng.geometric(np.clip(hazard, 1e-5, 0.5)) + 7
    cancel_day = np.where(cancel_day < days, cancel_day, -1)
    involuntary = (cancel_day >= 0) & (rng.random(n) < 0.3)

    subs = []
    for i, sid in enumerate(ids):
        f, l = FIRST[rng.integers(len(FIRST))], LAST[rng.integers(len(LAST))]
        _, city, zp = STATES[states[i]]
        subs.append({
            "subscriber_id": sid, "first_name": f, "last_name": l,
            "email": f"{f}.{l}{i}@example.com".lower(), "phone": f"(312) 555-01{i % 100:02d}",
            "ship_street": f"{100 + i} Example St", "ship_city": city, "ship_state": states[i],
            "ship_zip": f"{zp}{i % 100:02d}", "plan_interval": plan[i],
            "created_at": cfg.local_dt(start_day[i], 12 * 3600), "start_day": int(start_day[i]),
            "cancel_day": int(cancel_day[i]), "involuntary": bool(involuntary[i]),
            "propensity": float(propensity[i]),
        })
    subscribers = pd.DataFrame(subs)

    # ----------------------------------------------------------------- events
    orders, payments, tickets = [], [], []
    order_seq = 100000
    for i, sid in enumerate(ids):
        p = propensity[i]
        first_active = max(int(start_day[i]), 0)
        last_active = int(cancel_day[i]) if cancel_day[i] >= 0 else days - 1
        device = f"{sid}-device"
        sku = SKUS[rng.integers(len(SKUS))]
        if start_day[i] >= 0:
            hour = rng.uniform(15, 18) if start_day[i] == cfg.promo_day else rng.uniform(8, 22)
            ev.add("subscription_created", "subscription_platform", cfg.local_dt(start_day[i], hour * 3600), sid,
                   f'{{"plan": "{plan[i]}", "sku": "{sku[0]}"}}')

        interval = PLANS[plan[i]]
        phase = rng.integers(0, interval)
        paused_until = -1
        for d in range(first_active, last_active + 1):
            near_cancel = cancel_day[i] >= 0 and cancel_day[i] - d <= 14
            # Sessions (A-EVT-01, A-EVT-02); promo day multiplies traffic (A-EVT-04).
            dau = cfg.dau_share * (1.3 - p) * (0.6 if near_cancel else 1.0)
            if d == cfg.promo_day:
                dau = min(1.0, dau * cfg.peak_multiplier)
            if rng.random() < dau:
                src = "app" if rng.random() < 0.4 else "web"
                base = rng.uniform(7, 22) * 3600
                k = rng.poisson(cfg.events_per_dau)
                types = rng.choice(SESSION_TYPES[0], size=k, p=SESSION_TYPES[1])
                for j, t in enumerate(types):
                    ev.add(t, src, cfg.local_dt(d, base + j * 20), sid, f'{{"path": "/{t.replace("_", "-")}"}}', device)
                cancel_visit = 0.01 + (0.5 if near_cancel and not involuntary[i] else 0) + 0.05 * p
                if rng.random() < cancel_visit:
                    ev.add("cancel_page_view", src, cfg.local_dt(d, base + k * 20 + 30), sid, '{"path": "/account/cancel"}', device)
            # Messaging (A-EVT-03 minus billing/lifecycle, ~1.5/day).
            sent = rng.poisson(1.0)
            opened = rng.binomial(sent, 0.45 * (1 - p))
            for t, cnt in (("email_sent", sent), ("email_opened", opened), ("email_clicked", rng.binomial(opened, 0.2)),
                           ("sms_sent", rng.poisson(0.15))):
                for _ in range(cnt):
                    ev.add(t, "esp" if t.startswith("email") else "sms", cfg.local_dt(d, rng.uniform(9, 20) * 3600), sid,
                           '{"campaign": "lifecycle"}')
            # Pauses.
            if d > paused_until and rng.random() < 0.0007 * (1 + 3 * p):
                ev.add("subscription_paused", "subscription_platform", cfg.local_dt(d, 14 * 3600), sid, '{"weeks": 4}')
                paused_until = d + 28
                if paused_until <= last_active:
                    ev.add("subscription_resumed", "subscription_platform", cfg.local_dt(paused_until, 6 * 3600), sid, "{}")
            # Renewal cycle: skip, or charge and ship.
            if (d - phase) % interval == 0 and d > paused_until:
                if rng.random() < 0.08 + 0.3 * p:
                    ev.add("subscription_skipped", "subscription_platform", cfg.local_dt(d, 6 * 3600), sid, "{}")
                else:
                    fail = rng.random() < 0.03 + 0.1 * p
                    pay_id = f"pay_{len(payments):07d}"
                    amount = round(float(rng.uniform(28, 55)), 2)
                    when = cfg.local_dt(d, 5 * 3600)
                    payments.append({"payment_id": pay_id, "subscriber_id": sid, "attempted_at": when,
                                     "status": "failed" if fail else "succeeded", "amount": amount})
                    ev.add("payment_failed" if fail else "payment_succeeded", "billing", when, sid,
                           f'{{"payment_id": "{pay_id}"}}')
                    if not fail:
                        order_seq += 1
                        orders.append({"order_id": f"ORD-{order_seq}", "subscriber_id": sid, "order_date": when,
                                       "sku": sku[0], "product": sku[1], "product_category": sku[2],
                                       "health_inferable": sku[3], "quantity": 1, "amount": amount})
            # Support contacts (A-TRN-01), more likely for high-propensity subscribers.
            contact_p = cfg.contact_rate / 30 * (0.5 + 0.5 * p / propensity.mean()) + (0.04 if near_cancel else 0)
            if rng.random() < contact_p:
                topic = "cancellation" if near_cancel and rng.random() < 0.6 else rng.choice(["shipping", "billing", "product"])
                tickets.append({"ticket_id": f"tkt_{len(tickets):06d}", "subscriber_id": sid, "topic": topic, "day": d,
                                "opened_at": cfg.local_dt(d, rng.uniform(8, 17) * 3600)})

        if cancel_day[i] >= 0:
            c = int(cancel_day[i])
            if involuntary[i]:
                for back in (6, 4, 2):
                    if c - back >= first_active:
                        when = cfg.local_dt(c - back, 5 * 3600)
                        pay_id = f"pay_{len(payments):07d}"
                        payments.append({"payment_id": pay_id, "subscriber_id": sid, "attempted_at": when,
                                         "status": "failed", "amount": 39.0})
                        ev.add("payment_failed", "billing", when, sid, f'{{"payment_id": "{pay_id}"}}')
                ev.add("subscription_cancelled", "billing", cfg.local_dt(c, 6 * 3600), sid, '{"reason": "dunning"}')
            else:
                ev.add("subscription_cancelled", "subscription_platform", cfg.local_dt(c, 15 * 3600), sid,
                       '{"reason": "customer_request"}')

    # Anonymous (non-subscriber) traffic.
    n_anon = int(len(ev.cols["event_id"]) * cfg.anonymous_share)
    for k in range(n_anon):
        d = int(rng.integers(0, days))
        ev.add("page_view", "web", cfg.local_dt(d, rng.uniform(0, 86_400)), None, '{"path": "/plans"}', f"anon-{k % 5000}")

    events = ev.frame().sort_values("event_time", ignore_index=True)

    # ----------------------------------------------------------- helpdesk text
    tk = pd.DataFrame(tickets)
    sub_ix = subscribers.set_index("subscriber_id")
    last_order = pd.DataFrame(orders).groupby("subscriber_id")[["order_id", "product"]].last() if orders else pd.DataFrame()
    texts, closed = [], []
    for row in tk.itertuples():
        s = sub_ix.loc[row.subscriber_id]
        order_no, product = (last_order.loc[row.subscriber_id].tolist() if row.subscriber_id in last_order.index
                             else ("ORD-000000", "Daily Multivitamin"))
        texts.append(_transcript(row.topic, f"{s.first_name} {s.last_name}", s.email, s.phone, order_no, product, rng))
        closed.append(row.opened_at + timedelta(hours=float(rng.uniform(0.2, 20))))
    tk["transcript_raw"] = texts
    tk["closed_at"] = closed
    # Only tickets closed before the nightly build are visible to it.
    build_time = datetime.combine(cfg.end_date, time(5), cfg.zone)
    tk = tk[tk["closed_at"] < build_time].reset_index(drop=True)

    consent = pd.DataFrame({
        "subscriber_id": ids,
        "analytics_objection": False,
        "ai_processing": True,
        "health_data": rng.random(n) < 0.4,
        "deletion_pending": False,
        "updated_at": [cfg.local_dt(max(s, 0), 12 * 3600) for s in start_day],
    })

    return World(cfg, subscribers, pd.DataFrame(orders), pd.DataFrame(payments), consent, tk, events,
                 notes={"build_time": build_time})
