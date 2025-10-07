# -*- coding: utf-8 -*-
# Copyright 2026 PySquad Informatics LLP.

import secrets

_WEAK_ACTION_SECRETS = frozenset({
    '',
    'change-me',
    'change-this-action-secret',
})


def post_init_hook(env):
    """Ensure interactive-button HMAC secret is strong after install."""
    ensure_action_secret(env)


def ensure_action_secret(env):
    """Replace weak/default action secrets with a random value.

    Safe to call on install, upgrade, or first request that needs signing.
    """
    ICP = env['ir.config_parameter'].sudo()
    current = (ICP.get_param('pys_mm_odoo_ai.action_secret') or '').strip()
    if current in _WEAK_ACTION_SECRETS:
        ICP.set_param('pys_mm_odoo_ai.action_secret', secrets.token_urlsafe(32))
