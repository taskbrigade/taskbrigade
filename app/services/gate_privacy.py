"""DZ 13: trusted query sensitivity, Laplace noise and transactional dataset release budgets.

See docs/data-store.md → Noise and query budgets for the reviewed-query contract. Noise alone cannot make
arbitrary scripts differentially private. All numeric cells (including counts and metrics) form ONE vector;
the configured sensitivity bounds its total L1 change under add/remove-one-person adjacency.
"""
import hashlib
import json
import math
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Conflict, Forbidden, Unprocessable
from app.i18n import _
from app.models import Actor, Dataset, Project
from app.services import data_audit, datasets, gate_checks

ZERO = Decimal("0")
UNIT = Decimal("0.000001")
MAX_DECIMAL = Decimal("1000000000000")
RNG = secrets.SystemRandom()


def _invalid():
    return Unprocessable(_("Invalid output policy: use nonnegative limits and positive epsilon/sensitivity, "
                           "with at most six decimal places. Epsilon and sensitivity must be set together; "
                           "an epsilon limit requires noise."), code="invalid_gate_policy")


def _decimal(value, optional=True) -> Decimal | None:
    if value is None or value == "":
        if optional:
            return None
        raise _invalid()
    if isinstance(value, bool):
        raise _invalid()
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number < ZERO or number > MAX_DECIMAL
                or number != number.quantize(UNIT)):
            raise _invalid()
        return number.quantize(UNIT)
    except (InvalidOperation, ValueError, TypeError) as e:
        raise _invalid() from e


@dataclass(frozen=True)
class Policy:
    query_limit: int | None = None
    epsilon_limit: Decimal | None = None
    epsilon: Decimal | None = None
    sensitivity: Decimal | None = None

    @classmethod
    def parse(cls, fields: dict):
        raw = fields.get("query_limit")
        limit = None
        if raw is not None and raw != "":
            if isinstance(raw, bool) or not str(raw).isascii() or not str(raw).isdigit():
                raise _invalid()
            limit = int(raw)
            if limit > 2147483647:
                raise _invalid()
        epsilon_limit = _decimal(fields.get("epsilon_limit"))
        epsilon, sensitivity = _decimal(fields.get("epsilon")), _decimal(fields.get("sensitivity"))
        if ((epsilon is None) != (sensitivity is None) or epsilon == ZERO or sensitivity == ZERO
                or (epsilon_limit is not None and epsilon is None)):
            raise _invalid()
        if epsilon is not None and sensitivity / epsilon < Decimal("0.01"):
            raise Unprocessable(_("Noise scale (sensitivity/epsilon) must be at least 0.01 for two-decimal output"),
                                code="invalid_gate_policy")
        return cls(limit, epsilon_limit, epsilon, sensitivity)

    def fields(self) -> dict:
        return {"query_limit": self.query_limit,
                "epsilon_limit": str(self.epsilon_limit) if self.epsilon_limit is not None else None,
                "epsilon": str(self.epsilon) if self.epsilon is not None else None,
                "sensitivity": str(self.sensitivity) if self.sensitivity is not None else None}

    def token(self) -> str:
        return hashlib.sha256(json.dumps(self.fields(), sort_keys=True).encode()).hexdigest()


def policy(ds: Dataset | None) -> Policy:
    try:
        return Policy.parse(json.loads(ds.gate_policy_json or "{}")) if ds else Policy()
    except (ValueError, TypeError, AttributeError) as e:
        raise _invalid() from e  # Invalid stored policy fails closed, never silently disables protection


def lock_dataset(session: Session, dataset_id: int) -> Dataset:
    # Refresh an identity-map entry too: a competing release/policy edit may have committed while we waited.
    ds = session.scalar(select(Dataset).where(Dataset.id == dataset_id).with_for_update()
                        .execution_options(populate_existing=True))
    if ds is None:
        raise Conflict(_("The result's dataset no longer exists"), code="gate_dataset_missing")
    return ds


def configure(session: Session, ds: Dataset, actor: Actor, fields: dict) -> None:
    project = session.get(Project, ds.project_id)
    if not datasets.can_delete(project, actor):
        raise Forbidden(_("Only the project owner or an admin may change output policies"), code="gate_policy_forbidden")
    new = Policy.parse(fields)
    ds = lock_dataset(session, ds.id)
    if ds.status == "deleted":
        raise Conflict(_("Output policies cannot be changed for deleted datasets"), code="dataset_deleted")
    old = policy(ds)
    ds.gate_policy_json = json.dumps(new.fields())
    # Usage never resets when a policy is loosened, disabled or re-enabled.
    data_audit.append(session, "gate_policy_changed", "dataset", ds.id, actor=actor, via="web",
                      detail={"old": old.fields(), "new": new.fields()})


def exhausted(ds: Dataset | None, cfg: Policy) -> bool:
    count = (ds.released_count or 0) if ds else 0
    spent = (ds.epsilon_spent or ZERO) if ds else ZERO
    return ((cfg.query_limit is not None and count >= cfg.query_limit)
            or (cfg.epsilon_limit is not None and spent + (cfg.epsilon or ZERO) > cfg.epsilon_limit))


def view(ds: Dataset | None) -> dict:
    cfg = policy(ds)
    return {**cfg.fields(), "token": cfg.token(), "noise": cfg.epsilon is not None,
            "released_count": (ds.released_count or 0) if ds else 0,
            "epsilon_spent": str((ds.epsilon_spent or ZERO) if ds else ZERO),
            "exhausted": exhausted(ds, cfg)}


def laplace(scale: float) -> float:
    """Four-uniform Laplace sampler (Holohan & Braghin 2021, used by IBM diffprivlib).

    Avoids the reconstruction weakness of a single inverse-CDF draw. SystemRandom is backed by the OS;
    the implementation and its limits are documented, rather than claiming certified end-to-end DP.
    """
    return scale * sum(math.log1p(-RNG.random()) * math.cos(math.pi * RNG.random()) for _ in range(2))


def noisy_content(doc: dict, cfg: Policy) -> dict:
    content = gate_checks.release_content(doc)
    if cfg.epsilon is None:
        return content
    scale = float(cfg.sensitivity / cfg.epsilon)

    def perturb(value):
        if type(value) not in (int, float, bool):
            return value
        # Refuse values whose magnitudes cannot retain cents, rather than silently losing small noise.
        if abs(value) > 10**12:
            raise Unprocessable(_("Numeric output is too large for noise; revise the reviewed query"),
                                code="gate_noise_range")
        out = float(value) + laplace(scale)
        if not math.isfinite(out):
            raise Unprocessable(_("Noise could not produce a finite value"), code="gate_noise_range")
        return round(out, gate_checks.DECIMALS)

    for table in content["tables"]:
        table["rows"] = [[perturb(v) for v in row] for row in table["rows"]]
    content["metrics"] = {key: perturb(value) for key, value in content["metrics"].items()}
    return content


def release(session: Session, result, doc: dict, reviewed: bool, token: str | None) -> str:
    """Caller locks result/task first. Serialize and charge releases on the dataset row, in the same transaction
    as the result and audit. No commits here; rollback also restores usage. Reads never draw fresh noise."""
    ds = lock_dataset(session, result.dataset_id) if result.dataset_id is not None else None
    cfg = policy(ds)
    if exhausted(ds, cfg):
        raise Conflict(_("The dataset's output query budget is exhausted"), code="gate_budget_exhausted")
    if cfg.epsilon is not None and (not reviewed or token != cfg.token()):
        raise Conflict(_("Confirm the reviewed query's sensitivity and public output shape under the current "
                         "noise policy before release"), code="gate_noise_review_required")
    content = json.dumps(noisy_content(doc, cfg), ensure_ascii=False, allow_nan=False)
    if ds:
        ds.released_count = (ds.released_count or 0) + 1
        ds.epsilon_spent = (ds.epsilon_spent or ZERO) + (cfg.epsilon or ZERO)
    result.release_policy_json = json.dumps({**cfg.fields(), "mechanism": "laplace-4u/1" if cfg.epsilon else "none",
                                            "sensitivity_reviewed": bool(cfg.epsilon),
                                            "released_count": ds.released_count if ds else None,
                                            "epsilon_spent": str(ds.epsilon_spent) if ds else "0"})
    return content
