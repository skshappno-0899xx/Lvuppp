# -*- coding: utf-8 -*-
"""
levels.py — Free Fire Level/EXP helper
Per-level progress calculation for dashboard
"""

# Cumulative total EXP required to REACH each level
# Level 1 = 0 EXP (start), Level 100 = 32032284 EXP
LEVELS = {
    1: 0, 2: 48, 3: 202, 4: 544, 5: 1012, 6: 1844, 7: 2792, 8: 3800,
    9: 4870, 10: 6004, 11: 7192, 12: 8448, 13: 9776, 14: 11140, 15: 12566,
    16: 14060, 17: 15610, 18: 17224, 19: 18902, 20: 20632, 21: 22424,
    22: 24728, 23: 26192, 24: 28166, 25: 30200, 26: 32294, 27: 34448,
    28: 37804, 29: 41174, 30: 44870, 31: 48852, 32: 53334, 33: 58566,
    34: 64096, 35: 69994, 36: 76460, 37: 83108, 38: 91128, 39: 99322,
    40: 108092, 41: 120144, 42: 133266, 43: 147472, 44: 162760, 45: 179126,
    46: 196572, 47: 215368, 48: 235516, 49: 257010, 50: 279860, 51: 304056,
    52: 348318, 53: 394982, 54: 444044, 55: 495508, 56: 549364, 57: 633756,
    58: 721744, 59: 813336, 60: 908522, 61: 1041438, 62: 1180352, 63: 1325256,
    64: 1476184, 65: 1634300, 66: 1840946, 67: 2056594, 68: 2281242, 69: 2514880,
    70: 2757530, 71: 3059506, 72: 3372284, 73: 3699456, 74: 4041030, 75: 4397020,
    76: 4829104, 77: 5282204, 78: 5756304, 79: 6251404, 80: 6767504, 81: 7381324,
    82: 8043154, 83: 8752952, 84: 9510808, 85: 10316638, 86: 11277190, 87: 12360748,
    88: 13360304, 89: 14482858, 90: 15659418, 91: 17026708, 92: 18453688, 93: 19941280,
    94: 21488570, 95: 23095858, 96: 24763138, 97: 26490138, 98: 28277708, 99: 30124996,
    100: 32032284,
}

MAX_LEVEL = 100


def get_level_from_exp(exp: int) -> int:
    """Return the level number for a given cumulative EXP."""
    try:
        exp = int(exp or 0)
    except Exception:
        exp = 0
    if exp < 0:
        exp = 0
    level = 1
    for lvl in range(2, MAX_LEVEL + 1):
        if exp >= LEVELS[lvl]:
            level = lvl
        else:
            break
    return level


def get_level_progress(exp: int) -> dict:
    """
    Returns a dict with everything the dashboard needs:
      {
        "level": 39,
        "next_level": 40,
        "current_in_level": 726,
        "total_needed": 8770,
        "remaining": 8044,
        "percent": 8.28,
        "is_max": False
      }
    """
    try:
        exp = int(exp or 0)
    except Exception:
        exp = 0
    if exp < 0:
        exp = 0

    level = get_level_from_exp(exp)

    if level >= MAX_LEVEL:
        return {
            "level": MAX_LEVEL,
            "next_level": MAX_LEVEL,
            "current_in_level": 0,
            "total_needed": 0,
            "remaining": 0,
            "percent": 100.0,
            "is_max": True,
        }

    next_level = level + 1
    start_exp = LEVELS[level]
    next_exp = LEVELS[next_level]

    current_in_level = max(0, exp - start_exp)
    total_needed = max(1, next_exp - start_exp)
    remaining = max(0, total_needed - current_in_level)
    percent = round((current_in_level / total_needed) * 100, 2)

    return {
        "level": level,
        "next_level": next_level,
        "current_in_level": current_in_level,
        "total_needed": total_needed,
        "remaining": remaining,
        "percent": percent,
        "is_max": False,
    }


def format_uptime(seconds) -> str:
    """Format seconds into '9h 52m 31s' style string."""
    try:
        seconds = int(seconds or 0)
    except Exception:
        seconds = 0
    if seconds < 0:
        seconds = 0

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60

    if days > 0:
        return f"{days}d {hours}h {mins}m {secs}s"
    if hours > 0:
        return f"{hours}h {mins}m {secs}s"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"