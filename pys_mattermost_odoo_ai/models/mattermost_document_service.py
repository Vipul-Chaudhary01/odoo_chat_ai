# -*- coding: utf-8 -*-
import base64
import json
import logging
import mimetypes

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

from .mattermost_ai_service import CLAUDE_API_URL, ANTHROPIC_VERSION

_logger = logging.getLogger(__name__)

SUPPORTED_IMAGE_TYPES = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
}
SUPPORTED_DOC_TYPES = {
    'application/pdf',
}
MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MB


class MattermostDocumentService(models.AbstractModel):
    """Fetch Mattermost files and extract structured Odoo import data via Claude."""

    _name = 'mattermost.ai.document.service'
    _description = 'Mattermost AI Document Import Service'

    @api.model
    def _get_param(self, key, default=''):
        return self.env['ir.config_parameter'].sudo().get_param(key, default)

    @api.model
    def _mattermost_base_url(self):
        url = (self._get_param('pys_mm_odoo_ai.mattermost_url') or '').strip().rstrip('/')
        if not url:
            raise UserError(_(
                'Mattermost URL is not configured.\n'
                'Open Mattermost AI → Configuration and set Mattermost URL '
                '(e.g. http://localhost:8065).'
            ))
        return url

    @api.model
    def _mattermost_headers(self):
        token = (self._get_param('pys_mm_odoo_ai.bot_token') or '').strip()
        if not token:
            raise UserError(_(
                'Mattermost Bot Access Token is required for document import.\n'
                'Create a bot in Mattermost, invite it to the channel, then paste '
                'the token in Mattermost AI → Configuration.'
            ))
        return {
            'Authorization': 'Bearer %s' % token,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    @api.model
    def _mattermost_me(self):
        """Validate bot token and return bot user payload."""
        base = self._mattermost_base_url()
        headers = self._mattermost_headers()
        try:
            resp = requests.get('%s/api/v4/users/me' % base, headers=headers, timeout=20)
        except requests.exceptions.RequestException as exc:
            _logger.exception('Mattermost /users/me failed')
            raise UserError(_(
                'Could not reach Mattermost URL "%s". Check Mattermost URL in Configuration.'
            ) % base) from exc
        if resp.status_code == 401:
            raise UserError(_(
                'Mattermost Bot Token is invalid (HTTP 401).\n'
                'Create a new token for @odooai and paste it in Odoo Configuration.'
            ))
        if resp.status_code >= 400:
            raise UserError(_(
                'Mattermost authentication failed (HTTP %s). Check Bot Token.'
            ) % resp.status_code)
        return resp.json() or {}

    @api.model
    def _ensure_bot_in_channel(self, channel_id, bot_user_id):
        """Join the bot to the channel so it can read posts/files."""
        if not channel_id or not bot_user_id:
            return
        base = self._mattermost_base_url()
        headers = self._mattermost_headers()
        # Already a member?
        member_url = '%s/api/v4/channels/%s/members/%s' % (base, channel_id, bot_user_id)
        try:
            member_resp = requests.get(member_url, headers=headers, timeout=20)
            if member_resp.status_code == 200:
                return
            # Add bot to channel (works for public channels)
            add_url = '%s/api/v4/channels/%s/members' % (base, channel_id)
            add_resp = requests.post(
                add_url,
                headers=headers,
                data=json.dumps({'user_id': bot_user_id}),
                timeout=20,
            )
            if add_resp.status_code >= 400:
                _logger.warning(
                    'Could not auto-add bot to channel %s: HTTP %s %s',
                    channel_id, add_resp.status_code, add_resp.text[:200],
                )
        except requests.exceptions.RequestException:
            _logger.exception('Mattermost channel membership check/join failed')

    @api.model
    def fetch_channel_file(self, channel_id, file_ids=None):
        """Return file payload from explicit file_ids or latest channel file."""
        if file_ids:
            for file_id in file_ids:
                file_id = (file_id or '').strip()
                if file_id:
                    return self.download_file(file_id)
        return self.fetch_latest_channel_file(channel_id)

    @api.model
    def fetch_latest_channel_file(self, channel_id):
        """Return dict: filename, mimetype, content (bytes), file_id."""
        if not channel_id:
            raise UserError(_('Missing Mattermost channel id.'))

        me = self._mattermost_me()
        bot_user_id = me.get('id')
        self._ensure_bot_in_channel(channel_id, bot_user_id)

        base = self._mattermost_base_url()
        headers = self._mattermost_headers()
        posts_url = '%s/api/v4/channels/%s/posts' % (base, channel_id)
        try:
            resp = requests.get(posts_url, headers=headers, params={'per_page': 40}, timeout=30)
        except requests.exceptions.RequestException as exc:
            _logger.exception('Mattermost posts fetch failed')
            raise UserError(_('Could not reach Mattermost to fetch channel files.')) from exc

        if resp.status_code == 403:
            raise UserError(_(
                'Mattermost denied channel access (HTTP 403).\n\n'
                'Fix:\n'
                '1) Open Town Square → Add people → invite @odooai to this channel\n'
                '2) Ensure Bot Token in Odoo is the token of @odooai\n'
                '3) Upload the PDF as a normal message first\n'
                '4) Then run `/odoo import bill` (do not only attach file on the slash command)'
            ))
        if resp.status_code >= 400:
            raise UserError(_(
                'Mattermost file lookup failed (HTTP %s). Check Bot Token and channel access.'
            ) % resp.status_code)

        data = resp.json() or {}
        order = data.get('order') or []
        posts = data.get('posts') or {}
        file_id = None
        for post_id in order:
            post = posts.get(post_id) or {}
            # Skip posts from the slash-command response itself; prefer user uploads.
            post_file_ids = post.get('file_ids') or []
            if post_file_ids:
                file_id = post_file_ids[0]
                break
        if not file_id:
            raise UserError(_(
                'No recent file found in this channel.\n\n'
                'Correct steps:\n'
                '1) Upload PDF/image as a normal message (paperclip → send)\n'
                '2) Then type `/odoo import bill` and send (without relying on slash-command attachment)'
            ))
        return self.download_file(file_id)

    @api.model
    def download_file(self, file_id):
        base = self._mattermost_base_url()
        headers = self._mattermost_headers()
        info_url = '%s/api/v4/files/%s/info' % (base, file_id)
        file_url = '%s/api/v4/files/%s' % (base, file_id)
        try:
            info_resp = requests.get(info_url, headers=headers, timeout=30)
            file_resp = requests.get(file_url, headers=headers, timeout=60)
        except requests.exceptions.RequestException as exc:
            _logger.exception('Mattermost file download failed')
            raise UserError(_('Could not download the Mattermost file.')) from exc
        if info_resp.status_code >= 400 or file_resp.status_code >= 400:
            raise UserError(_('Mattermost denied file download. Check bot permissions.'))

        info = info_resp.json() or {}
        content = file_resp.content or b''
        if not content:
            raise UserError(_('Downloaded file is empty.'))
        if len(content) > MAX_FILE_BYTES:
            raise UserError(_('File is too large (max 8 MB).'))

        filename = info.get('name') or ('mattermost-file-%s' % file_id)
        mimetype = (info.get('mime_type') or mimetypes.guess_type(filename)[0] or '').lower()
        if mimetype not in SUPPORTED_IMAGE_TYPES | SUPPORTED_DOC_TYPES:
            raise UserError(_(
                'Unsupported file type "%s". Upload PDF, JPG, PNG, GIF, or WEBP.'
            ) % (mimetype or 'unknown'))
        return {
            'file_id': file_id,
            'filename': filename,
            'mimetype': mimetype,
            'content': content,
            'size': len(content),
        }

    @api.model
    def extract_bill_proposal(self, file_payload, import_kind='vendor_bill'):
        """Use Claude to read PDF/image and return account.move create proposal."""
        api_key = self._get_param('pys_mm_odoo_ai.anthropic_api_key')
        if not api_key:
            raise UserError(_('Anthropic API key is not configured.'))
        model = self._get_param('pys_mm_odoo_ai.claude_model', 'claude-sonnet-5') or 'claude-sonnet-5'
        timeout = int(self._get_param('pys_mm_odoo_ai.timeout', '90') or 90)

        mimetype = file_payload['mimetype']
        b64 = base64.b64encode(file_payload['content']).decode('ascii')
        if mimetype in SUPPORTED_IMAGE_TYPES:
            media_block = {
                'type': 'image',
                'source': {'type': 'base64', 'media_type': mimetype, 'data': b64},
            }
        else:
            media_block = {
                'type': 'document',
                'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': b64},
            }

        default_move = 'in_invoice' if import_kind in ('vendor_bill', 'bill', 'purchase') else 'out_invoice'
        system = (
            'You extract accounting documents into structured JSON for Odoo.\n'
            'Return ONLY valid JSON with this schema:\n'
            '{\n'
            '  "intent": "create",\n'
            '  "requires_confirmation": true,\n'
            '  "tool_name": "create_record",\n'
            '  "model": "account.move",\n'
            '  "values": {\n'
            '    "move_type": "in_invoice or out_invoice",\n'
            '    "partner_id": "Vendor or Customer name",\n'
            '    "ref": "invoice number if any",\n'
            '    "invoice_date": "YYYY-MM-DD or null",\n'
            '    "invoice_line_ids": [\n'
            '      {"name": "line description", "quantity": 1, "price_unit": 100.0, "product_name": ""}\n'
            '    ]\n'
            '  },\n'
            '  "summary": "short summary",\n'
            '  "user_message": "short Mattermost explanation"\n'
            '}\n'
            'Default move_type is %s unless the document clearly is a customer invoice.\n'
            'Use product_name only when a clear product title exists; otherwise put text in name.\n'
            'Do not invent totals that contradict line math.\n'
        ) % default_move

        payload = {
            'model': model,
            'max_tokens': 1200,
            'system': system,
            'messages': [{
                'role': 'user',
                'content': [
                    media_block,
                    {
                        'type': 'text',
                        'text': (
                            'Extract this document as an Odoo accounting import proposal. '
                            'Filename: %s'
                        ) % file_payload.get('filename'),
                    },
                ],
            }],
        }
        headers = {
            'x-api-key': api_key,
            'anthropic-version': ANTHROPIC_VERSION,
            'content-type': 'application/json',
        }
        try:
            response = requests.post(
                CLAUDE_API_URL,
                headers=headers,
                data=json.dumps(payload),
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise UserError(_('Document AI request timed out. Please try again.')) from exc
        except requests.exceptions.RequestException as exc:
            _logger.exception('Claude document extract failed')
            raise UserError(_('The AI service could not read this document.')) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise UserError(_('The AI service returned an invalid response.')) from exc
        if response.status_code >= 400:
            err = (data.get('error') or {}) if isinstance(data, dict) else {}
            message = err.get('message') if isinstance(err, dict) else str(err)
            _logger.warning('Claude document API error %s: %s', response.status_code, message)
            raise UserError(_('The AI could not read this document. Please try another file.'))

        text = self.env['mattermost.ai.service']._extract_text(data.get('content') or [])
        proposal = self.env['mattermost.ai.service']._parse_json(text)
        if not isinstance(proposal, dict):
            raise UserError(_('The AI could not extract data from this document.'))
        proposal.setdefault('intent', 'create')
        proposal.setdefault('tool_name', 'create_record')
        proposal.setdefault('model', 'account.move')
        proposal.setdefault('requires_confirmation', True)
        values = proposal.get('values') if isinstance(proposal.get('values'), dict) else {}
        values.setdefault('move_type', default_move)
        proposal['values'] = values
        return proposal, text, data.get('usage') or {}
