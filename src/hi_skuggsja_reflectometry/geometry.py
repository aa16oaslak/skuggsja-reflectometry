"""Angles of the setup, as in Fig. 1 of Singh et al., arXiv:2407.05512.

Seen from above: the transmitter (Tx) is fixed; the receiver (Rx) rides on
the large rotation stage (R1) and the sample on the small one (R2). All
angles are in degrees, measured from the Tx direction.

- sample angle: angle of the sample normal from Tx.
- receiver angle: angle between Tx and Rx as seen from the sample.
- phi1: angle of the sample normal from the Tx direction (the angle of
  incidence); phi2: angle of Rx from the sample normal. A specular geometry
  has phi2 == phi1, i.e. receiver = 2 x sample.

All are measured the same way round (the way the receiver angle grows) and
kept within one turn, whatever the stage counts are -- the sample stage
has no end stops, so its count can pass a full turn: the stage angles are
in [0, 360), phi1 and phi2 in (-180, 180], negative meaning the other way.
"""
from __future__ import annotations


def within_turn(angle: float) -> float:
    """`angle` in [0, 360)."""
    wrapped = angle % 360.0
    return 0.0 if wrapped > 360.0 - 1e-9 else wrapped


def signed(angle: float) -> float:
    """`angle` in (-180, 180]."""
    wrapped = within_turn(angle)
    return wrapped - 360.0 if wrapped > 180.0 else wrapped


def angle_between(a: float, b: float) -> float:
    """How far `a` is from `b`, the short way round, in (-180, 180]."""
    return signed(a - b)


def receiver_angle(calibrated_position: float, zero_l: float) -> float:
    """Receiver angle from the large stage's calibrated position (the large
    stage is mounted reversed, hence the sign)."""
    return within_turn(zero_l - calibrated_position)


def sample_angle(calibrated_position: float, zero_s: float) -> float:
    return within_turn(calibrated_position - zero_s)


def phi_angles(sample: float, receiver: float) -> tuple[float, float]:
    """(phi1, phi2): the sample normal from Tx (the angle of incidence), and
    the receiver from the sample normal, each the short way round."""
    return signed(sample), angle_between(receiver, sample)


def is_specular(sample: float, receiver: float, tol: float = 0.05) -> bool:
    return abs(angle_between(receiver, 2 * sample)) <= tol
