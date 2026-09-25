"""Angles of the setup, as in Fig. 1 of Singh et al., arXiv:2407.05512.

Seen from above: the transmitter (Tx) is fixed; the receiver (Rx) rides on
the large rotation stage (R1) and the sample on the small one (R2). All
angles are in degrees, measured from the Tx direction.

- sample angle: angle of the sample normal = angle of incidence, phi1.
- receiver angle: angle between Tx and Rx as seen from the sample.
- phi2 = receiver angle - sample angle: angle of Rx from the sample normal.
  A specular geometry has phi2 == phi1, i.e. receiver = 2 x sample.
"""
from __future__ import annotations


def receiver_angle(calibrated_position: float, zero_l: float) -> float:
    """Receiver angle from the large stage's calibrated position (the large
    stage is mounted reversed, hence the sign)."""
    return zero_l - calibrated_position


def sample_angle(calibrated_position: float, zero_s: float) -> float:
    return calibrated_position - zero_s


def phi_angles(sample: float, receiver: float) -> tuple[float, float]:
    """(phi1, phi2): angle of incidence and angle of the receiver, both from
    the sample normal."""
    return sample, receiver - sample


def is_specular(sample: float, receiver: float, tol: float = 0.05) -> bool:
    return abs(receiver - 2 * sample) <= tol
