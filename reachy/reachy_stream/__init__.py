# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Standalone slice of the Reachy Mini HA component: just the WebRTC stream client.

The upstream package __init__ imports homeassistant, so it cannot be imported outside HA. stream.py
and const.py have no HA imports, so vendoring the pair keeps Pollen signalling logic without
dragging in Home Assistant.
"""
