"""Quaternion / rotation helpers for splat Sim3 alignment."""

from __future__ import annotations

import numpy as np
from numba import njit, prange


@njit(cache=True)
def _quaternion_multiply_njit(
    w1: float,
    x1: float,
    y1: float,
    z1: float,
    w2: float,
    x2: float,
    y2: float,
    z2: float,
) -> tuple:
    """Multiply two quaternions [w, x, y, z]. Returns (w, x, y, z)."""
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


@njit(cache=True)
def _rotation_matrix_to_quaternion_njit(R: np.ndarray) -> tuple:
    """Convert 3x3 rotation matrix to quaternion (w, x, y, z)."""
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0:
        s = 0.5 / np.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (R[2, 1] - R[1, 2]) * s
        y = (R[0, 2] - R[2, 0]) * s
        z = (R[1, 0] - R[0, 1]) * s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    return (w, x, y, z)


@njit(cache=True, parallel=True)
def transform_quaternions_batch(
    rot_0: np.ndarray,
    rot_1: np.ndarray,
    rot_2: np.ndarray,
    rot_3: np.ndarray,
    rw: float,
    rx: float,
    ry: float,
    rz: float,
    out_0: np.ndarray,
    out_1: np.ndarray,
    out_2: np.ndarray,
    out_3: np.ndarray,
) -> None:
    """Pre-multiply all splat quaternions by rotation quaternion (rw, rx, ry, rz)."""
    n = len(rot_0)
    for i in prange(n):
        qw, qx, qy, qz = rot_0[i], rot_1[i], rot_2[i], rot_3[i]
        nw, nx, ny, nz = _quaternion_multiply_njit(rw, rx, ry, rz, qw, qx, qy, qz)
        norm = np.sqrt(nw * nw + nx * nx + ny * ny + nz * nz)
        out_0[i] = nw / norm
        out_1[i] = nx / norm
        out_2[i] = ny / norm
        out_3[i] = nz / norm


def quaternion_to_rotation_matrix(q: np.ndarray) -> np.ndarray:
    """Convert quaternion [w, x, y, z] to 3x3 rotation matrix."""
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * z * w, 2 * x * z + 2 * y * w],
            [2 * x * y + 2 * z * w, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * x * w],
            [2 * x * z - 2 * y * w, 2 * y * z + 2 * x * w, 1 - 2 * x * x - 2 * y * y],
        ]
    )


def rotation_matrix_to_quaternion(R: np.ndarray) -> np.ndarray:
    """Convert 3x3 rotation matrix to quaternion [w, x, y, z]."""
    w, x, y, z = _rotation_matrix_to_quaternion_njit(np.ascontiguousarray(R, dtype=np.float64))
    return np.array([w, x, y, z])


def quaternion_multiply(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """Multiply two quaternions [w, x, y, z]."""
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    return np.array(
        _quaternion_multiply_njit(float(w1), float(x1), float(y1), float(z1), float(w2), float(x2), float(y2), float(z2))
    )


def rotation_matrix_z(angle_deg: float) -> np.ndarray:
    """Create a 3x3 rotation matrix for rotation around Z axis."""
    angle_rad = np.deg2rad(angle_deg)
    c, s = np.cos(angle_rad), np.sin(angle_rad)
    return np.array(
        [
            [c, -s, 0],
            [s, c, 0],
            [0, 0, 1],
        ]
    )


# Splats are saved with a fixed Z pre-rotation; compose before alignment.
PRE_ROTATION_Z_DEG = -90.0
PRE_ROTATION_MATRIX = rotation_matrix_z(PRE_ROTATION_Z_DEG)
