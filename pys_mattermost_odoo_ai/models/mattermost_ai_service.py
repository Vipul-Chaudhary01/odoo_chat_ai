# -*- coding: utf-8 -*-
import json
import logging

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CLAUDE_API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
CLAUDE_MODELS = [
    ('claude-sonnet-5', 'Claude Sonnet 5'),
    ('claude-haiku-4-5', 'Claude Haiku 4.5'),
    ('claude-sonnet-4-6', 'Claude Sonnet 4.6'),
]


class MattermostAIService(models.AbstractModel):
    """Anthropic Claude client used only to understand intent and propose tools."""

    _name = 'mattermost.ai.service'
    _description = 'Mattermost AI Claude Service'

    @api.model
    def _get_param(self, key, default=''):
        return self.env['ir.config_parameter'].sudo().get_param(key, default)

    @api.model
    def propose_operation(self, prompt, allowed_models):
        api_key = self._get_param('pys_mm_odoo_ai.anthropic_api_key')
        if not api_key:
            raise UserError(_(
                'Anthropic API key is not configured.\n'
                'Open Mattermost AI → Configuration and set the API key.'
            ))
        model = self._get_param('pys_mm_odoo_ai.claude_model', 'claude-sonnet-5') or 'claude-sonnet-5'
        timeout = int(self._get_param('pys_mm_odoo_ai.timeout', '60') or 60)

        system = (
            'You are an Odoo business assistant operating through Mattermost.\n'
            'Understand the user natural-language request and convert it into ONE structured operation.\n'
            'Do NOT execute anything. Do NOT invent record ids unless the user provided them.\n'
            'Allowed models: %s\n'
            'IMPORTANT: You MUST support EVERY model listed above. '
            'If purchase.order is listed, purchase orders ARE supported. '
            'If crm.lead is listed, CRM IS supported. '
            'If account.move is listed, Accounting invoices/bills ARE supported. '
            'Never say a listed model is unsupported.\n'
            'Allowed tools: search_records, get_record, create_record, write_record, execute_action.\n'
            'Never propose unlink/delete. Never use ir.* or res.users.\n'
            'If the request is a search/read, set requires_confirmation=false.\n'
            'If the request creates, updates, or runs a workflow action, set requires_confirmation=true.\n'
            'Return ONLY valid JSON with this schema:\n'
            '{\n'
            '  "intent": "search|create|update|action|clarify",\n'
            '  "requires_confirmation": boolean,\n'
            '  "tool_name": "search_records|get_record|create_record|write_record|execute_action",\n'
            '  "model": "technical.model.name",\n'
            '  "record_id": integer or null,\n'
            '  "domain": [],\n'
            '  "values": {},\n'
            '  "method": "action_confirm or null",\n'
            '  "lookup": {"name": "", "email": ""},\n'
            '  "response_fields": ["id"] or ["name","email"] — ONLY the fields the user asked to see;\n'
            '                 leave empty/null for a normal full summary,\n'
            '  "summary": "short human summary",\n'
            '  "user_message": "short explanation for Mattermost"\n'
            '}\n'
            'If the user says they want only id / only price / only email / etc., '
            'set response_fields to exactly those fields (use id, name, email, phone, '
            'list_price, default_code as applicable).\n'
            'For customers/contacts use res.partner.\n'
            'For products use product.template or product.product.\n'
            'For quotations/orders use sale.order with values like:\n'
            '{"partner_id": "Customer Name", "order_line": '
            '[{"product_name": "Product Name", "product_uom_qty": 2}]}.\n'
            'For purchase orders use purchase.order with values like:\n'
            '{"partner_id": "Vendor Name", "order_line": '
            '[{"product_name": "Product Name", "product_qty": 5}]}.\n'
            'For CRM use crm.lead with values like:\n'
            '{"name": "Opportunity title", "type": "opportunity", '
            '"email_from": "a@b.com", "partner_id": "Customer Name"}.\n'
            'For Accounting invoices/bills use account.move with values like:\n'
            '{"move_type": "out_invoice", "partner_id": "Customer Name", '
            '"invoice_line_ids": [{"product_name": "Product", "quantity": 1, "price_unit": 100}]}\n'
            'or move_type=in_invoice for vendor bills.\n'
            'Use names when ids are unknown; Odoo will resolve them.\n'
            'To create a Regular invoice from a sales order, use intent=action, '
            'model=sale.order, method=_create_invoices, and set record_id or '
            'lookup.name to the SO number (e.g. S00005).\n'
            'To post a draft invoice/bill, use intent=action, model=account.move, '
            'method=action_post with the invoice name/id.\n'
        ) % ', '.join(allowed_models)

        payload = {
            'model': model,
            'max_tokens': 800,
            'system': system,
            'messages': [{'role': 'user', 'content': prompt}],
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
            raise UserError(_('The AI request timed out. Please try again.')) from exc
        except requests.exceptions.RequestException as exc:
            _logger.exception('Claude API request failed')
            raise UserError(_('The AI service could not be reached. Please try again.')) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise UserError(_('The AI service returned an invalid response.')) from exc

        if response.status_code >= 400:
            err = (data.get('error') or {}) if isinstance(data, dict) else {}
            message = err.get('message') if isinstance(err, dict) else str(err)
            _logger.warning('Claude API error %s: %s', response.status_code, message)
            raise UserError(_('The AI service could not process this request. Please try again.'))

        text = self._extract_text(data.get('content') or [])
        proposal = self._parse_json(text)
        if not isinstance(proposal, dict):
            raise UserError(_('The AI could not understand that request. Please rephrase it.'))
        return proposal, text, data.get('usage') or {}

    @api.model
    def _extract_text(self, content_blocks):
        texts = []
        for block in content_blocks or []:
            if isinstance(block, dict) and block.get('type') == 'text':
                texts.append(block.get('text') or '')
        return '\n'.join(texts).strip()

    @api.model
    def _parse_json(self, text):
        if not text:
            raise UserError(_('The AI returned an empty proposal.'))
        start = text.find('{')
        end = text.rfind('}')
        if start < 0 or end <= start:
            raise UserError(_('The AI could not understand that request. Please rephrase it.'))
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise UserError(_('The AI could not understand that request. Please rephrase it.')) from exc
