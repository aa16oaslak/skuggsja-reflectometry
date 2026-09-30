import pytest

from hi_skuggsja_reflectometry import geometry


@pytest.mark.parametrize("angle, turn, signed", [
    (0, 0, 0), (381.53, 21.53, 21.53), (-3, 357, -3), (180, 180, 180), (190, 190, -170), (-360, 0, 0), (720.5, 0.5, 0.5),
])
def test_angles_are_kept_within_one_turn(angle, turn, signed):
    assert geometry.within_turn(angle) == pytest.approx(turn)
    assert geometry.signed(angle) == pytest.approx(signed)


def test_the_lab_screenshot():
    # sample stage count 341.53 (past a full turn), receiver at 112.09°:
    # the window showed phi1 381.53° and phi2 -269.44°
    sample = geometry.sample_angle(341.53, zero_s=-40)
    receiver = geometry.receiver_angle(68.41, zero_l=180.5)

    assert (sample, receiver) == (pytest.approx(21.53), pytest.approx(112.09))
    assert geometry.phi_angles(sample, receiver) == (pytest.approx(21.53), pytest.approx(90.56))


def test_phi_angles_near_the_wrap():
    # normal 10° the other way from Tx, receiver 20° from Tx: incidence -10°, receiver 30° from the normal
    assert geometry.phi_angles(350, 20) == (pytest.approx(-10), pytest.approx(30))


def test_specular_across_a_full_turn():
    assert geometry.is_specular(20, 40)
    assert geometry.is_specular(200, 40)  # 2 x 200° is 40° after a full turn
    assert not geometry.is_specular(20, 45)
