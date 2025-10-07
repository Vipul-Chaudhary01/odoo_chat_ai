# -*- coding: utf-8 -*-
import json

import requests

from odoo import _, api, fields, models
from odoo.http import request

from ..hooks import ensure_action_secret
from .mattermost_ai_service import CLAUDE_API_URL, ANTHROPIC_VERSION, CLAUDE_MODELS


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    mm_ai_enabled = fields.Boolean(
        string='Enable Mattermost → Odoo AI',
        config_parameter='pys_mm_odoo_ai.enabled',
    )
    mm_ai_slash_command_token = fields.Char(
        string='Slash Command Token',
        config_parameter='pys_mm_odoo_ai.slash_command_token',
    )
    mm_ai_mattermost_url = fields.Char(
        string='Mattermost URL',
        config_parameter='pys_mm_odoo_ai.mattermost_url',
        help='Required for document import file download, e.g. http://localhost:8065',
    )
    mm_ai_bot_token = fields.Char(
        string='Bot Access Token',
        config_parameter='pys_mm_odoo_ai.bot_token',
        help='Required for `/odoo import bill` to download channel files from Mattermost.',
    )
    mm_ai_anthropic_api_key = fields.Char(
        string='Anthropic API Key',
        config_parameter='pys_mm_odoo_ai.anthropic_api_key',
    )
    mm_ai_claude_model = fields.Selection(
        selection=CLAUDE_MODELS,
        string='Claude Model',
        config_parameter='pys_mm_odoo_ai.claude_model',
        default='claude-sonnet-5',
    )
    mm_ai_confirm_writes = fields.Boolean(
        string='Require Confirmation for Write Operations',
        config_parameter='pys_mm_odoo_ai.confirm_writes',
        default=True,
    )
    mm_ai_odoo_base_url = fields.Char(
        string='Public Odoo URL',
        config_parameter='pys_mm_odoo_ai.odoo_base_url',
        help=(
            'URL Mattermost uses for Confirm/Cancel button callbacks. '
            'If Mattermost runs in Docker, use the Docker bridge host '
            '(e.g. http://172.17.0.1:1919), not http://localhost:1919.'
        ),
    )
    mm_ai_allowed_models_auto = fields.Text(
        string='Allowed Models (from installed Apps)',
        compute='_compute_mm_ai_permission_info',
        readonly=True,
    )
    mm_ai_allowed_actions_auto = fields.Text(
        string='Allowed Actions (from installed Apps)',
        compute='_compute_mm_ai_permission_info',
        readonly=True,
    )
    mm_ai_app_coverage = fields.Text(
        string='App Coverage',
        compute='_compute_mm_ai_permission_info',
        readonly=True,
    )
    mm_ai_extra_models = fields.Char(
        string='Extra Models (optional)',
        config_parameter='pys_mm_odoo_ai.extra_models',
        help='Comma-separated technical model names to allow in addition to installed Apps.',
    )
    mm_ai_extra_actions = fields.Char(
        string='Extra Actions (optional)',
        config_parameter='pys_mm_odoo_ai.extra_actions',
        help='Comma-separated method names to allow in addition to installed Apps.',
    )

    @api.depends_context('uid')
    def _compute_mm_ai_permission_info(self):
        Tools = self.env['mattermost.ai.tools']
        models = Tools._get_allowed_models()
        actions = sorted(Tools._get_allowed_actions())
        coverage_lines = []
        for row in Tools._get_app_model_coverage():
            status = 'ON' if row['active'] else 'OFF'
            model_txt = ', '.join(row['models']) if row['models'] else '-'
            coverage_lines.append('%s [%s]: %s' % (row['module'], status, model_txt))
        for rec in self:
            rec.mm_ai_allowed_models_auto = ', '.join(models) if models else ''
            rec.mm_ai_allowed_actions_auto = ', '.join(actions) if actions else ''
            rec.mm_ai_app_coverage = '\n'.join(coverage_lines)

    def set_values(self):
        super().set_values()
        ensure_action_secret(self.env)

    def action_mm_ai_test_configuration(self):
        """Validate required config values and show a checklist notification."""
        self.ensure_one()
        self.set_values()
        params = self.env['ir.config_parameter'].sudo()

        lines = []
        ok_count = 0
        fail_count = 0

        def add(ok, label, detail=''):
            nonlocal ok_count, fail_count
            if ok:
                ok_count += 1
                lines.append('✅ %s%s' % (label, (' — %s' % detail) if detail else ''))
            else:
                fail_count += 1
                lines.append('❌ %s%s' % (label, (' — %s' % detail) if detail else ''))

        enabled = (params.get_param('pys_mm_odoo_ai.enabled') or '').lower() in ('1', 'true', 'yes', 'on')
        add(enabled, 'Integration enabled')

        token = (params.get_param('pys_mm_odoo_ai.slash_command_token') or '').strip()
        add(bool(token), 'Slash Command Token', 'set (%s chars)' % len(token) if token else 'missing')

        api_key = (params.get_param('pys_mm_odoo_ai.anthropic_api_key') or '').strip()
        add(bool(api_key), 'Anthropic API Key', 'set' if api_key else 'missing')

        model = params.get_param('pys_mm_odoo_ai.claude_model') or 'claude-sonnet-5'
        add(bool(model), 'Claude model', model)

        mapping_count = self.env['mattermost.ai.user.mapping'].sudo().search_count([
            ('active', '=', True),
            ('allowed_execute', '=', True),
        ])
        add(
            mapping_count > 0,
            'Active user mapping',
            '%s mapping(s)' % mapping_count if mapping_count else 'create one under User Mappings',
        )

        allowed_models = self.env['mattermost.ai.tools']._get_allowed_models()
        add(bool(allowed_models), 'Allowed models from Apps', ', '.join(allowed_models[:8]) + ('...' if len(allowed_models) > 8 else ''))
        for row in self.env['mattermost.ai.tools']._get_app_model_coverage():
            if row['module'] in ('purchase', 'crm', 'account', 'sale', 'product'):
                lines.append(
                    '%s App %s → %s'
                    % (
                        '✅' if row['active'] else 'ℹ️',
                        row['module'],
                        ('enabled: ' + ', '.join(row['models'])) if row['active'] else 'not installed',
                    )
                )

        public_url = (params.get_param('pys_mm_odoo_ai.odoo_base_url') or '').strip()
        if public_url:
            loopback = 'localhost' in public_url.lower() or '127.0.0.1' in public_url
            if loopback:
                lines.append(
                    '⚠️ Public Odoo URL uses localhost (%s) — Confirm buttons may fail '
                    'from Mattermost Docker; prefer http://172.17.0.1:1919'
                    % public_url
                )
            else:
                add(True, 'Public Odoo URL', public_url)
        else:
            lines.append('ℹ️ Public Odoo URL empty — buttons fall back to request host URL')

        try:
            base = (request.httprequest.host_url or 'http://127.0.0.1:1919/').rstrip('/')
            url = '%s/mattermost/command' % base
            resp = requests.post(
                url,
                data={
                    'token': 'invalid-test-token',
                    'user_id': 'test',
                    'user_name': 'test',
                    'text': 'Find ABC',
                    'command': '/odoo',
                    'channel_id': 'test',
                    'team_id': 'test',
                },
                timeout=8,
            )
            body = resp.text or ''
            endpoint_ok = resp.status_code == 200 and (
                'Unauthorized' in body or 'disabled' in body.lower() or 'mapped' in body.lower()
            )
            add(
                endpoint_ok,
                'Odoo /mattermost/command endpoint',
                'HTTP %s' % resp.status_code if endpoint_ok else 'HTTP %s — restart Odoo / check addons path' % resp.status_code,
            )
        except Exception as exc:  # noqa: BLE001
            add(False, 'Odoo /mattermost/command endpoint', str(exc)[:120])

        if api_key:
            try:
                timeout = int(params.get_param('pys_mm_odoo_ai.timeout', '60') or 60)
                resp = requests.post(
                    CLAUDE_API_URL,
                    headers={
                        'x-api-key': api_key,
                        'anthropic-version': ANTHROPIC_VERSION,
                        'content-type': 'application/json',
                    },
                    data=json.dumps({
                        'model': model,
                        'max_tokens': 16,
                        'messages': [{'role': 'user', 'content': 'Reply with exactly: OK'}],
                    }),
                    timeout=min(timeout, 30),
                )
                if resp.status_code < 400:
                    add(True, 'Anthropic API connection', 'OK')
                else:
                    err = ''
                    try:
                        err = (resp.json().get('error') or {}).get('message') or resp.text
                    except Exception:  # noqa: BLE001
                        err = resp.text
                    add(False, 'Anthropic API connection', 'HTTP %s: %s' % (resp.status_code, (err or '')[:160]))
            except Exception as exc:  # noqa: BLE001
                add(False, 'Anthropic API connection', str(exc)[:160])

        message = '\n'.join(lines)
        notif_type = 'success' if fail_count == 0 else ('warning' if ok_count else 'danger')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Mattermost AI Configuration Test (%s OK / %s fail)') % (ok_count, fail_count),
                'message': message,
                'type': notif_type,
                'sticky': True,
            },
        }
