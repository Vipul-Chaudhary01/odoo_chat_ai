# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import timedelta

from odoo import api, fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..hooks import ensure_action_secret
from ..models.mattermost_ai_tools import ACTION_METHOD_ALIASES, READ_TOOLS, WRITE_TOOLS

_logger = logging.getLogger(__name__)


def _truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


class MattermostOdooAIController(http.Controller):

    @http.route('/mattermost/command', type='http', auth='public', methods=['POST'], csrf=False)
    def mattermost_command(self, **_kwargs):
        try:
            return self._mattermost_command()
        except Exception:  # noqa: BLE001
            _logger.exception('Mattermost slash command failed')
            return self._json_response({
                'response_type': 'ephemeral',
                'text': '### ❌ Odoo Operation Failed\n\nThe requested operation could not be completed.',
            })

    @http.route('/mattermost/action', type='http', auth='public', methods=['POST'], csrf=False)
    def mattermost_action(self, **_kwargs):
        try:
            return self._mattermost_action()
        except Exception:  # noqa: BLE001
            _logger.exception('Mattermost action failed')
            return self._json_response({
                'update': {
                    'message': '### ❌ Odoo Operation Failed\n\nThe requested operation could not be completed.',
                }
            })

    def _mattermost_command(self):
        env = request.env
        params = env['ir.config_parameter'].sudo()
        if not _truthy(params.get_param('pys_mm_odoo_ai.enabled', 'False')):
            return self._json_response({
                'response_type': 'ephemeral',
                'text': 'Mattermost → Odoo AI integration is currently disabled.',
            })

        form = request.httprequest.form
        token = form.get('token') or ''
        expected = params.get_param('pys_mm_odoo_ai.slash_command_token', '') or ''
        # Mattermost shows "command failed" unless HTTP 200 + JSON is returned.
        if not expected or not self._tokens_match(token, expected):
            return self._json_response({
                'response_type': 'ephemeral',
                'text': (
                    'Unauthorized Mattermost request.\n\n'
                    'Paste the Slash Command Token from Mattermost into '
                    'Odoo → Mattermost AI → Configuration, then Save.'
                ),
            })

        user_id = (form.get('user_id') or '').strip()
        user_name = (form.get('user_name') or '').strip()
        channel_id = (form.get('channel_id') or '').strip()
        channel_name = (form.get('channel_name') or '').strip()
        team_id = (form.get('team_id') or '').strip()
        command = (form.get('command') or '').strip() or '/odoo'
        text = (form.get('text') or '').strip()

        if not user_id:
            return self._json_response({
                'response_type': 'ephemeral',
                'text': 'Unauthorized Mattermost request. Missing user id.',
            })

        mapping = self._get_mapping(env, user_id)
        if not mapping:
            return self._json_response({
                'response_type': 'ephemeral',
                'text': (
                    'Your Mattermost account is not mapped to an Odoo user.\n'
                    'Please contact the administrator.'
                ),
            })
        if not mapping.allowed_execute:
            return self._json_response({
                'response_type': 'ephemeral',
                'text': 'You do not have permission to perform this Odoo operation.',
            })

        if not text:
            return self._json_response({
                'response_type': 'ephemeral',
                'text': (
                    '**Usage**\n'
                    '- `/odoo Create a customer named ABC Pvt Ltd`\n'
                    '- `/odoo Find ABC Pvt Ltd`\n'
                    '- `/odoo Update ABC Pvt Ltd phone to 9876543210`\n'
                    '- `/odoo import bill`  _(upload PDF/image in channel first)_\n'
                    '- `/odoo import invoice`\n'
                    '- `/odoo confirm MM-AI-XXXXXXXX`\n'
                    '- `/odoo cancel MM-AI-XXXXXXXX`'
                ),
            })

        lower = text.lower()
        if lower.startswith('confirm '):
            request_name = text.split(None, 1)[1].strip()
            return self._confirm_request(env, request_name, user_id, mapping, source='command')
        if lower.startswith('cancel '):
            request_name = text.split(None, 1)[1].strip()
            return self._cancel_request(env, request_name, user_id, source='command')
        if lower.startswith('import ') or lower in ('import bill', 'import invoice', 'import document'):
            form = request.httprequest.form
            raw_file_ids = form.get('file_ids') or form.get('file_id') or ''
            file_ids = [p.strip() for p in str(raw_file_ids).replace(';', ',').split(',') if p.strip()]
            return self._handle_document_import(
                env,
                mapping=mapping,
                prompt=text,
                user_id=user_id,
                user_name=user_name,
                team_id=team_id,
                channel_id=channel_id,
                channel_name=channel_name,
                command=command,
                file_ids=file_ids,
            )

        return self._handle_prompt(
            env,
            mapping=mapping,
            prompt=text,
            user_id=user_id,
            user_name=user_name,
            team_id=team_id,
            channel_id=channel_id,
            channel_name=channel_name,
            command=command,
        )

    def _mattermost_action(self):
        env = request.env
        params = env['ir.config_parameter'].sudo()
        if not _truthy(params.get_param('pys_mm_odoo_ai.enabled', 'False')):
            return self._json_response({
                'ephemeral_text': 'Mattermost → Odoo AI integration is currently disabled.',
            })

        payload = self._json_body()
        context = payload.get('context') or {}
        user_id = (payload.get('user_id') or '').strip()
        action = (context.get('action') or '').strip()
        request_name = (context.get('request_id') or '').strip()
        signature = (context.get('sig') or '').strip()

        if not self._verify_sig(params, action, request_name, signature):
            return self._json_response({'ephemeral_text': 'Unauthorized Mattermost request.'})

        mapping = self._get_mapping(env, user_id)
        if not mapping or not mapping.allowed_execute:
            return self._json_response({
                'ephemeral_text': 'You do not have permission to perform this Odoo operation.',
            })

        if action == 'cancel':
            return self._cancel_request(env, request_name, user_id, source='action')
        if action == 'confirm':
            return self._confirm_request(env, request_name, user_id, mapping, source='action')
        return self._json_response({'ephemeral_text': 'Unknown action.'})

    def _handle_document_import(self, env, mapping, prompt, user_id, user_name, team_id, channel_id, channel_name, command, file_ids=None):
        params = env['ir.config_parameter'].sudo()
        expiry_minutes = int(params.get_param('pys_mm_odoo_ai.request_expiry_minutes', '15') or 15)
        lower = (prompt or '').lower()
        import_kind = 'customer_invoice' if 'invoice' in lower and 'bill' not in lower else 'vendor_bill'

        if 'account.move' not in env['mattermost.ai.tools']._get_allowed_models():
            return self._json_response({
                'response_type': 'ephemeral',
                'text': (
                    '### 🤖 Odoo AI\n\n'
                    'Accounting is not available. Install the Invoicing/Accounting app first.'
                ),
            })

        req = env['mattermost.ai.request'].sudo().create({
            'state': 'draft',
            'mattermost_user_id': user_id,
            'mattermost_username': user_name,
            'team_id': team_id,
            'channel_id': channel_id,
            'channel_name': channel_name,
            'command': command,
            'odoo_user_id': mapping.odoo_user_id.id,
            'mapping_id': mapping.id,
            'prompt': prompt,
            'expires_at': fields.Datetime.now() + timedelta(minutes=expiry_minutes),
        })

        try:
            Doc = env['mattermost.ai.document.service'].sudo()
            file_payload = Doc.fetch_channel_file(channel_id, file_ids=file_ids or [])
            proposal, raw_text, usage = Doc.extract_bill_proposal(file_payload, import_kind=import_kind)
            env['ir.attachment'].sudo().create({
                'name': file_payload.get('filename') or 'mattermost-document',
                'datas': base64.b64encode(file_payload['content']),
                'res_model': 'mattermost.ai.request',
                'res_id': req.id,
                'mimetype': file_payload.get('mimetype'),
            })
        except UserError as exc:
            req.write({
                'state': 'failed',
                'error_message': str(exc),
                'completed_at': fields.Datetime.now(),
            })
            return self._json_response({
                'response_type': 'ephemeral',
                'text': '### ❌ Document Import Failed\n\n%s' % str(exc),
            })

        user_env = api.Environment(env.cr, mapping.odoo_user_id.id, dict(env.context))
        try:
            values = user_env['mattermost.ai.tools']._prepare_values(
                'account.move', proposal.get('values') or {},
            )
        except UserError as exc:
            req.write({
                'state': 'failed',
                'error_message': str(exc),
                'ai_proposal_text': raw_text,
                'proposal_json': json.dumps(proposal, default=str),
                'completed_at': fields.Datetime.now(),
            })
            return self._json_response({
                'response_type': 'ephemeral',
                'text': '### ❌ Document Import Failed\n\n%s' % str(exc),
            })

        req.write({
            'state': 'proposed',
            'ai_proposal_text': raw_text,
            'proposal_json': json.dumps(proposal, default=str),
            'tool_name': 'create_record',
            'tool_arguments': json.dumps({'model': 'account.move', 'values': values}, default=str),
            'target_model': 'account.move',
            'operation_type': 'create',
            'requires_confirmation': True,
            'summary': (proposal.get('summary') or 'Document import')[:200],
            'user_message': proposal.get('user_message') or proposal.get('summary') or (
                'Imported from document %s' % (file_payload.get('filename') or '')
            ),
            'claude_input_tokens': usage.get('input_tokens') or 0,
            'claude_output_tokens': usage.get('output_tokens') or 0,
        })
        return self._json_response(self._proposal_payload(env, req, mapping))

    def _handle_prompt(self, env, mapping, prompt, user_id, user_name, team_id, channel_id, channel_name, command):
        params = env['ir.config_parameter'].sudo()
        expiry_minutes = int(params.get_param('pys_mm_odoo_ai.request_expiry_minutes', '15') or 15)
        confirm_writes = _truthy(params.get_param('pys_mm_odoo_ai.confirm_writes', 'True'))
        user_env = api.Environment(env.cr, mapping.odoo_user_id.id, dict(env.context))
        allowed_models = user_env['mattermost.ai.tools']._get_allowed_models()

        req = env['mattermost.ai.request'].sudo().create({
            'state': 'draft',
            'mattermost_user_id': user_id,
            'mattermost_username': user_name,
            'team_id': team_id,
            'channel_id': channel_id,
            'channel_name': channel_name,
            'command': command,
            'odoo_user_id': mapping.odoo_user_id.id,
            'mapping_id': mapping.id,
            'prompt': prompt,
            'expires_at': fields.Datetime.now() + timedelta(minutes=expiry_minutes),
        })

        try:
            proposal, raw_text, usage = env['mattermost.ai.service'].sudo().propose_operation(
                prompt, allowed_models,
            )
        except UserError as exc:
            req.write({
                'state': 'failed',
                'error_message': str(exc),
                'completed_at': fields.Datetime.now(),
            })
            return self._json_response({
                'response_type': 'ephemeral',
                'text': '### ❌ Odoo Operation Failed\n\n%s' % str(exc),
            })

        tool_name, arguments, intent, requires_confirmation, summary, user_message = self._normalize_proposal(
            user_env, proposal, confirm_writes, prompt=prompt,
        )
        req.write({
            'ai_proposal_text': raw_text,
            'proposal_json': json.dumps(proposal, default=str),
            'tool_name': tool_name,
            'tool_arguments': json.dumps(arguments, default=str),
            'target_model': arguments.get('model'),
            'target_record_id': arguments.get('record_id') or 0,
            'operation_type': intent if intent in ('search', 'create', 'update', 'action', 'clarify') else False,
            'requires_confirmation': requires_confirmation,
            'summary': (summary or '')[:200],
            'user_message': user_message,
            'claude_input_tokens': usage.get('input_tokens') or 0,
            'claude_output_tokens': usage.get('output_tokens') or 0,
        })

        if intent == 'clarify' or not tool_name:
            req.write({
                'state': 'failed',
                'error_message': user_message or 'Could not understand the request.',
                'completed_at': fields.Datetime.now(),
            })
            return self._json_response({
                'response_type': 'ephemeral',
                'text': '### 🤖 Odoo AI\n\n%s\n\nPlease rephrase the request.' % (
                    user_message or 'I could not map that request to a safe Odoo operation.'
                ),
            })

        if not requires_confirmation:
            req.write({'state': 'proposed'})
            if not req._claim_for_execution():
                return self._already_processed_response('command')
            return self._run_and_respond(req, user_env, source='command', immediate_read=True)

        req.write({'state': 'proposed'})
        return self._json_response(self._proposal_payload(env, req, mapping))

    def _normalize_proposal(self, user_env, proposal, confirm_writes, prompt=''):
        Tools = user_env['mattermost.ai.tools']
        intent = (proposal.get('intent') or '').strip().lower()
        tool_name = (proposal.get('tool_name') or '').strip()
        model = (proposal.get('model') or '').strip()
        values = proposal.get('values') if isinstance(proposal.get('values'), dict) else {}
        lookup = proposal.get('lookup') if isinstance(proposal.get('lookup'), dict) else {}
        domain = proposal.get('domain') if isinstance(proposal.get('domain'), list) else []
        record_id = proposal.get('record_id') or None
        method = (proposal.get('method') or '').strip() or None
        summary = proposal.get('summary') or ''
        user_message = proposal.get('user_message') or summary
        response_fields = self._resolve_response_fields(prompt, proposal)

        if intent == 'search' and tool_name not in READ_TOOLS:
            tool_name = 'search_records'
        if intent == 'create':
            tool_name = 'create_record'
        if intent == 'update':
            tool_name = 'write_record'
        if intent == 'action':
            tool_name = 'execute_action'
            method = ACTION_METHOD_ALIASES.get(method or '', method)
            if not method and ('invoice' in (prompt or '').lower() or 'invoic' in (summary or '').lower()):
                method = '_create_invoices'

        if tool_name not in READ_TOOLS | WRITE_TOOLS:
            return False, {}, 'clarify', False, summary, user_message or 'Unsupported operation.'

        if model not in Tools._get_allowed_models():
            return False, {}, 'clarify', False, summary, 'That Odoo model is not allowed.'

        if tool_name == 'search_records' and not domain:
            name = lookup.get('name')
            email = lookup.get('email')
            if name:
                domain.append(['name', 'ilike', name])
            if email:
                domain.append(['email', 'ilike', email])
            if not domain and values.get('name'):
                domain.append(['name', 'ilike', values.get('name')])

        if tool_name in ('write_record', 'execute_action') and not record_id:
            record_id = self._resolve_record_id(Tools, model, lookup, values, domain)
            if not record_id and model == 'sale.order':
                # e.g. "invoice for S00005"
                so_name = lookup.get('name') or values.get('name')
                if not so_name:
                    match = re.search(r'\b(S\d{3,})\b', prompt or '', flags=re.IGNORECASE)
                    if match:
                        so_name = match.group(1).upper()
                if so_name:
                    record_id = self._resolve_named_record_id(Tools, 'sale.order', so_name)
            if not record_id and model == 'account.move':
                inv_name = lookup.get('name') or values.get('name')
                if not inv_name:
                    match = re.search(r'\b((INV|BILL|RINV|RBILL)[A-Z0-9/|-]+)\b', prompt or '', flags=re.IGNORECASE)
                    if match:
                        inv_name = match.group(1)
                if inv_name:
                    record_id = self._resolve_named_record_id(Tools, 'account.move', inv_name)
            if not record_id:
                return False, {}, 'clarify', False, summary, (
                    'I could not uniquely identify the Odoo record to update. '
                    'Please include a more specific name or email.'
                )

        arguments = {'model': model}
        if tool_name == 'search_records':
            arguments.update({'domain': domain or [], 'limit': 10})
            if response_fields:
                arguments['response_fields'] = response_fields
                orm_fields = [f for f in response_fields if f != 'display_name']
                if 'id' not in orm_fields:
                    orm_fields = ['id'] + orm_fields
                arguments['fields'] = orm_fields
        elif tool_name == 'get_record':
            arguments['record_id'] = int(record_id or 0)
            if response_fields:
                arguments['response_fields'] = response_fields
        elif tool_name == 'create_record':
            try:
                values = Tools._prepare_values(model, values)
            except UserError as exc:
                return False, {}, 'clarify', False, summary, str(exc)
            arguments['values'] = values
            if response_fields:
                arguments['response_fields'] = response_fields
        elif tool_name == 'write_record':
            try:
                values = Tools._prepare_values(model, values)
            except UserError as exc:
                return False, {}, 'clarify', False, summary, str(exc)
            arguments.update({'record_id': int(record_id or 0), 'values': values})
            if response_fields:
                arguments['response_fields'] = response_fields
        elif tool_name == 'execute_action':
            arguments.update({'record_id': int(record_id or 0), 'method': method})

        requires_confirmation = tool_name in WRITE_TOOLS and confirm_writes
        if tool_name in READ_TOOLS:
            intent = 'search'
            requires_confirmation = False
        return tool_name, arguments, intent, requires_confirmation, summary, user_message

    def _resolve_response_fields(self, prompt, proposal):
        """Return only the fields the user asked to see (e.g. only id)."""
        alias = {
            'id': 'id',
            'ids': 'id',
            'name': 'name',
            'email': 'email',
            'phone': 'phone',
            'mobile': 'mobile',
            'price': 'list_price',
            'list_price': 'list_price',
            'sku': 'default_code',
            'code': 'default_code',
            'default_code': 'default_code',
            'state': 'state',
            'total': 'amount_total',
            'amount': 'amount_total',
            'amount_total': 'amount_total',
        }
        fields = []
        raw = proposal.get('response_fields') if isinstance(proposal, dict) else None
        if isinstance(raw, list):
            for item in raw:
                key = alias.get(str(item or '').strip().lower())
                if key and key not in fields:
                    fields.append(key)
        text = (prompt or '').lower()
        # "only id", "just the id", "i need only id", "id only"
        only_match = False
        for token, field in alias.items():
            patterns = (
                r'\bonly\s+(the\s+)?%s\b' % token,
                r'\bjust\s+(the\s+)?%s\b' % token,
                r'\b%s\s+only\b' % token,
                r'\bneed\s+(to\s+)?only\s+%s\b' % token,
                r'\bonly\s+%s\b' % token,
            )
            if any(re.search(p, text) for p in patterns):
                only_match = True
                if field not in fields:
                    fields.append(field)
        if only_match and fields:
            return fields
        return fields or []

    def _resolve_named_record_id(self, Tools, model_name, name):
        result = Tools.execute_tool('search_records', {
            'model': model_name,
            'domain': [['name', '=', name]],
            'fields': ['id', 'name'],
            'limit': 1,
        })
        records = (result or {}).get('records') or []
        if len(records) == 1:
            return records[0].get('id')
        result = Tools.execute_tool('search_records', {
            'model': model_name,
            'domain': [['name', 'ilike', name]],
            'fields': ['id', 'name'],
            'limit': 2,
        })
        records = (result or {}).get('records') or []
        if len(records) == 1:
            return records[0].get('id')
        return 0

    def _resolve_record_id(self, Tools, model, lookup, values, domain):
        search_domain = list(domain or [])
        if lookup.get('name'):
            search_domain.append(['name', 'ilike', lookup.get('name')])
        elif values.get('name'):
            search_domain.append(['name', 'ilike', values.get('name')])
        if lookup.get('email') and model == 'res.partner':
            search_domain.append(['email', 'ilike', lookup.get('email')])
        if not search_domain:
            return 0
        result = Tools.execute_tool('search_records', {
            'model': model,
            'domain': search_domain,
            'fields': ['id', 'name'],
            'limit': 2,
        })
        records = (result or {}).get('records') or []
        if len(records) == 1:
            return records[0].get('id')
        return 0

    def _confirm_request(self, env, request_name, user_id, mapping, source):
        req = env['mattermost.ai.request'].sudo().search([('name', '=', request_name)], limit=1)
        if not req:
            return self._action_or_command_text(source, 'Operation not found.')
        if req.mattermost_user_id != user_id:
            return self._action_or_command_text(source, 'You cannot confirm someone else’s request.')
        if req.state in ('done', 'executing', 'confirmed'):
            return self._action_or_command_text(source, 'This operation has already been processed.')
        if req.state == 'cancelled':
            return self._action_or_command_text(source, 'This operation was cancelled.')
        if req.state == 'expired' or req._is_expired():
            if req.state != 'expired':
                req.write({'state': 'expired', 'completed_at': fields.Datetime.now()})
            return self._action_or_command_text(source, 'This operation has expired. Please submit the request again.')
        if req.state != 'proposed':
            return self._action_or_command_text(source, 'This operation has already been processed.')
        if not mapping or mapping.odoo_user_id != req.odoo_user_id or not mapping.allowed_execute:
            return self._action_or_command_text(source, 'You do not have permission to perform this Odoo operation.')

        if not req._claim_for_execution():
            return self._action_or_command_text(source, 'This operation has already been processed.')

        req.write({'confirmed_by_mm_user_id': user_id})
        user_env = api.Environment(env.cr, mapping.odoo_user_id.id, dict(env.context))
        return self._run_and_respond(req, user_env, source=source, immediate_read=False)

    def _cancel_request(self, env, request_name, user_id, source):
        req = env['mattermost.ai.request'].sudo().search([('name', '=', request_name)], limit=1)
        if not req:
            return self._action_or_command_text(source, 'Operation not found.')
        if req.mattermost_user_id != user_id:
            return self._action_or_command_text(source, 'You cannot cancel someone else’s request.')
        if req.state in ('done', 'executing'):
            return self._action_or_command_text(source, 'This operation has already been processed.')
        if req.state not in ('draft', 'proposed', 'confirmed'):
            return self._action_or_command_text(source, 'This operation cannot be cancelled.')
        req.write({'state': 'cancelled', 'completed_at': fields.Datetime.now()})
        text = '### 🚫 Operation cancelled.\n\nNo Odoo record was changed.'
        if source == 'action':
            return self._json_response({'update': {'message': text, 'props': {}}})
        return self._json_response({'response_type': 'ephemeral', 'text': text})

    def _run_and_respond(self, req, user_env, source, immediate_read=False):
        try:
            result = req.execute_stored_proposal(user_env)
        except AccessError:
            req.write({
                'state': 'failed',
                'error_message': 'Access denied',
                'completed_at': fields.Datetime.now(),
            })
            return self._action_or_command_text(source, 'You do not have permission to perform this Odoo operation.')
        except Exception as exc:  # noqa: BLE001
            _logger.exception('Stored proposal execution failed')
            req.write({
                'state': 'failed',
                'error_message': str(exc),
                'completed_at': fields.Datetime.now(),
            })
            return self._action_or_command_text(
                source,
                'The requested Odoo operation could not be completed.',
            )

        if not result.get('ok'):
            access = result.get('access_denied')
            message = (
                'You do not have permission to perform this Odoo operation.'
                if access else
                'The requested Odoo operation could not be completed.\n\nReason:\n%s' % (
                    result.get('error') or 'Unknown error'
                )
            )
            req.write({
                'state': 'failed',
                'error_message': result.get('error') or message,
                'result_json': json.dumps(result, default=str),
                'completed_at': fields.Datetime.now(),
            })
            return self._action_or_command_text(source, message)

        pretty = self._format_success(req, result)
        record_id = result.get('record_id') or 0
        req.write({
            'state': 'done',
            'result_text': pretty,
            'result_json': json.dumps(result, default=str),
            'target_record_id': record_id or req.target_record_id,
            'executed_at': fields.Datetime.now(),
            'completed_at': fields.Datetime.now(),
        })
        title = '### ✅ Odoo Operation Completed' if not immediate_read else '### 🔎 Odoo Search Result'
        text = '%s\n\n%s\n\n%s' % (title, self._prompt_block(req), pretty)
        if source == 'action':
            return self._json_response({'update': {'message': text, 'props': {}}})
        return self._json_response({'response_type': 'in_channel', 'text': text})

    @staticmethod
    def _prompt_block(req):
        """Mattermost does not post slash-command text; echo it in the bot reply."""
        prompt = (req.prompt or '').strip()
        if not prompt:
            return ''
        return '**Your request:**\n> %s' % prompt.replace('\n', '\n> ')

    def _proposal_payload(self, env, req, mapping):
        arguments = json.loads(req.tool_arguments or '{}')
        values = arguments.get('values') or {}
        details = []
        if req.target_model:
            details.append('**Model:** `%s`' % req.target_model)
        if req.tool_name:
            details.append('**Operation:** `%s`' % req.tool_name)
        if values.get('name'):
            details.append('**Name:** %s' % values.get('name'))
        if values.get('email'):
            details.append('**Email:** %s' % values.get('email'))
        if values.get('phone'):
            details.append('**Phone:** %s' % values.get('phone'))
        if values.get('partner_id'):
            details.append('**Customer ID:** `%s`' % values.get('partner_id'))
        if values.get('order_line'):
            line_bits = []
            for line in values.get('order_line') or []:
                line_vals = line[2] if isinstance(line, (list, tuple)) and len(line) >= 3 else line
                if isinstance(line_vals, dict):
                    line_bits.append(
                        'product_id=%s qty=%s'
                        % (line_vals.get('product_id'), line_vals.get('product_uom_qty'))
                    )
            if line_bits:
                details.append('**Order lines:** %s' % ', '.join(line_bits))
        extra_vals = {
            k: v for k, v in values.items()
            if k not in ('name', 'email', 'phone', 'partner_id', 'order_line') and v not in (None, False, '')
        }
        if extra_vals:
            details.append('**Values:** `%s`' % extra_vals)
        if arguments.get('record_id'):
            details.append('**Record ID:** %s' % arguments.get('record_id'))
        if arguments.get('method'):
            details.append('**Action:** `%s`' % arguments.get('method'))

        body = (
            '### 🤖 Odoo AI Operation\n\n'
            '%s\n\n'
            '**Requested by:** @%s\n'
            '**Request ID:** `%s`\n\n'
            '%s\n\n'
            '%s\n\n'
            '⚠️ This operation will change data in Odoo.\n\n'
            'Click **Confirm** / **Cancel**, or reply:\n'
            '`/odoo confirm %s`\n'
            '`/odoo cancel %s`'
        ) % (
            self._prompt_block(req),
            req.mattermost_username or mapping.mattermost_username or 'user',
            req.name,
            req.user_message or req.summary or 'Please confirm this operation.',
            '\n'.join(details),
            req.name,
            req.name,
        )
        base_url = self._callback_base_url(env)
        if base_url and self._is_loopback_url(base_url):
            body += (
                '\n\n_Note: Public Odoo URL points at localhost. Confirm/Cancel buttons '
                'often fail when Mattermost runs in Docker — use the `/odoo confirm` '
                'command above, or set Public Odoo URL to a host Mattermost can reach '
                '(e.g. `http://172.17.0.1:1919`)._'
            )
        payload = {
            'response_type': 'ephemeral',
            'text': body,
        }
        actions = self._button_actions(env, req)
        if actions:
            payload['attachments'] = [{
                'text': 'Confirm this Odoo operation?',
                'actions': actions,
            }]
        return payload

    def _callback_base_url(self, env):
        params = env['ir.config_parameter'].sudo()
        base_url = (params.get_param('pys_mm_odoo_ai.odoo_base_url') or '').rstrip('/')
        if not base_url:
            base_url = (request.httprequest.host_url or '').rstrip('/')
        return base_url

    @staticmethod
    def _is_loopback_url(url):
        lowered = (url or '').lower()
        return 'localhost' in lowered or '127.0.0.1' in lowered

    def _button_actions(self, env, req):
        base_url = self._callback_base_url(env)
        if not base_url:
            return []
        callback = '%s/mattermost/action' % base_url
        params = env['ir.config_parameter'].sudo()
        return [
            {
                'id': 'confirm',
                'name': 'Confirm',
                'type': 'button',
                'style': 'primary',
                'integration': {
                    'url': callback,
                    'context': {
                        'action': 'confirm',
                        'request_id': req.name,
                        'sig': self._sign(params, 'confirm', req.name),
                    },
                },
            },
            {
                'id': 'cancel',
                'name': 'Cancel',
                'type': 'button',
                'style': 'danger',
                'integration': {
                    'url': callback,
                    'context': {
                        'action': 'cancel',
                        'request_id': req.name,
                        'sig': self._sign(params, 'cancel', req.name),
                    },
                },
            },
        ]

    def _format_success(self, req, result):
        try:
            arguments = json.loads(req.tool_arguments or '{}')
        except json.JSONDecodeError:
            arguments = {}
        response_fields = arguments.get('response_fields') or []

        record = result.get('record') or {}
        if req.tool_name == 'search_records':
            records = result.get('records') or []
            if not records:
                return 'No matching records found.'
            if response_fields:
                return self._format_fields_only(records, response_fields)
            lines = ['**Found %s record(s)**' % (result.get('count') or len(records))]
            for rec in records[:10]:
                lines.append('- %s' % self._format_record_line(req.target_model, rec))
            return '\n'.join(lines)

        lines = []
        if req.operation_type == 'create' or result.get('created'):
            lines.append('Record created successfully.')
        elif req.operation_type == 'update' or result.get('updated'):
            lines.append('Record updated successfully.')

        if response_fields:
            # Single-record create/update/get: only requested fields
            payload = record or {'id': result.get('record_id')}
            if result.get('record_id') and 'id' not in payload:
                payload = dict(payload)
                payload['id'] = result.get('record_id')
            return '\n'.join(
                ([lines[0]] if lines else []) + [self._format_fields_only([payload], response_fields)]
            )

        if result.get('invoice_ids') or (req.tool_name == 'execute_action' and result.get('model') == 'account.move'):
            lines.append(result.get('message') or 'Invoice created successfully.')
            if record.get('name'):
                lines.append('**Invoice:** %s' % record.get('name'))
            if result.get('record_id'):
                lines.append('**Odoo ID:** `%s`' % result.get('record_id'))
            if record.get('amount_total') not in (None, False, ''):
                lines.append('**Total:** %s' % record.get('amount_total'))
            if record.get('state'):
                lines.append('**State:** %s' % record.get('state'))
            if req.odoo_user_id:
                lines.append('**Executed by:** %s' % req.odoo_user_id.display_name)
            return '\n'.join(lines)

        lines.extend(self._format_record_details(req.target_model, record))
        rid = result.get('record_id') or record.get('id')
        if rid and not any(line.startswith('**Odoo ID:**') for line in lines):
            lines.append('**Odoo ID:** `%s`' % rid)
        if req.target_model:
            lines.append('**Model:** `%s`' % req.target_model)
        if req.odoo_user_id:
            lines.append('**Executed by:** %s' % req.odoo_user_id.display_name)
        return '\n'.join(lines) or (result.get('message') or 'Done.')

    def _format_fields_only(self, records, response_fields):
        """Return only the fields the user asked for."""
        labels = {
            'id': 'Odoo ID',
            'name': 'Name',
            'email': 'Email',
            'phone': 'Phone',
            'mobile': 'Mobile',
            'list_price': 'Price',
            'default_code': 'SKU',
            'state': 'State',
            'amount_total': 'Total',
        }
        lines = []
        for rec in records[:10]:
            parts = []
            for field in response_fields:
                value = rec.get(field)
                if field == 'phone' and value in (None, False, ''):
                    value = rec.get('mobile')
                if isinstance(value, dict):
                    value = value.get('name') or value.get('id')
                if value in (None, False, ''):
                    continue
                if field == 'id' and len(response_fields) == 1:
                    parts.append('`%s`' % value)
                else:
                    parts.append('**%s:** `%s`' % (labels.get(field, field), value))
            if parts:
                # Single field "only id" → just the id value / labeled once
                if len(response_fields) == 1 and response_fields[0] == 'id':
                    lines.append('**Odoo ID:** %s' % parts[0])
                elif len(records) == 1:
                    lines.extend(parts)
                else:
                    lines.append('- ' + ' | '.join(parts))
        return '\n'.join(lines) or 'No matching values found.'

    def _format_record_line(self, model_name, rec):
        name = rec.get('name') or rec.get('display_name') or rec.get('id')
        rid = rec.get('id')
        model_name = model_name or ''
        if model_name.startswith('product.'):
            bits = ['**%s** (ID `%s`)' % (name, rid)]
            code = rec.get('default_code')
            price = rec.get('list_price')
            if code:
                bits.append('SKU=%s' % code)
            if price not in (None, False, ''):
                bits.append('price=%s' % price)
            return ' '.join(bits)
        if model_name == 'sale.order':
            bits = ['**%s** (ID `%s`)' % (name, rid)]
            if rec.get('state'):
                bits.append('state=%s' % rec.get('state'))
            amount = rec.get('amount_total')
            if amount not in (None, False, ''):
                bits.append('total=%s' % amount)
            partner = rec.get('partner_id')
            if isinstance(partner, dict) and partner.get('name'):
                bits.append('customer=%s' % partner.get('name'))
            return ' '.join(bits)
        if model_name == 'account.move':
            bits = ['**%s** (ID `%s`)' % (name, rid)]
            if rec.get('move_type'):
                bits.append('type=%s' % rec.get('move_type'))
            if rec.get('state'):
                bits.append('state=%s' % rec.get('state'))
            amount = rec.get('amount_total')
            if amount not in (None, False, ''):
                bits.append('total=%s' % amount)
            partner = rec.get('partner_id')
            if isinstance(partner, dict) and partner.get('name'):
                bits.append('partner=%s' % partner.get('name'))
            return ' '.join(bits)
        if model_name == 'purchase.order':
            bits = ['**%s** (ID `%s`)' % (name, rid)]
            if rec.get('state'):
                bits.append('state=%s' % rec.get('state'))
            amount = rec.get('amount_total')
            if amount not in (None, False, ''):
                bits.append('total=%s' % amount)
            partner = rec.get('partner_id')
            if isinstance(partner, dict) and partner.get('name'):
                bits.append('vendor=%s' % partner.get('name'))
            return ' '.join(bits)
        # Contacts / default
        bits = ['**%s** (ID `%s`)' % (name, rid)]
        if rec.get('email'):
            bits.append('email=%s' % rec.get('email'))
        phone = rec.get('phone') or rec.get('mobile')
        if phone:
            bits.append('phone=%s' % phone)
        return ' '.join(bits)

    def _format_record_details(self, model_name, record):
        lines = []
        name = record.get('name') or record.get('display_name')
        if name:
            lines.append('**Name:** %s' % name)
        model_name = model_name or ''
        if model_name.startswith('product.'):
            if record.get('default_code'):
                lines.append('**SKU:** %s' % record.get('default_code'))
            if record.get('list_price') not in (None, False, ''):
                lines.append('**Price:** %s' % record.get('list_price'))
        elif model_name == 'sale.order':
            partner = record.get('partner_id')
            if isinstance(partner, dict) and partner.get('name'):
                lines.append('**Customer:** %s' % partner.get('name'))
            if record.get('state'):
                lines.append('**State:** %s' % record.get('state'))
            if record.get('amount_total') not in (None, False, ''):
                lines.append('**Total:** %s' % record.get('amount_total'))
        elif model_name == 'account.move':
            partner = record.get('partner_id')
            if isinstance(partner, dict) and partner.get('name'):
                lines.append('**Partner:** %s' % partner.get('name'))
            if record.get('move_type'):
                lines.append('**Type:** %s' % record.get('move_type'))
            if record.get('state'):
                lines.append('**State:** %s' % record.get('state'))
            if record.get('amount_total') not in (None, False, ''):
                lines.append('**Total:** %s' % record.get('amount_total'))
        else:
            if record.get('email'):
                lines.append('**Email:** %s' % record.get('email'))
            if record.get('phone') or record.get('mobile'):
                lines.append('**Phone:** %s' % (record.get('phone') or record.get('mobile')))
        return lines

    def _get_mapping(self, env, mattermost_user_id):
        return env['mattermost.ai.user.mapping'].sudo().search([
            ('mattermost_user_id', '=', mattermost_user_id),
            ('active', '=', True),
        ], limit=1)

    def _action_secret(self, params):
        ensure_action_secret(params.env)
        return params.get_param('pys_mm_odoo_ai.action_secret') or ''

    def _sign(self, params, action, request_name):
        secret = self._action_secret(params)
        msg = ('%s:%s' % (action, request_name)).encode()
        return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()

    def _verify_sig(self, params, action, request_name, signature):
        expected = self._sign(params, action, request_name)
        signature = str(signature or '')
        expected = str(expected or '')
        if not signature or len(signature) != len(expected):
            return False
        try:
            return hmac.compare_digest(signature, expected)
        except (TypeError, ValueError):
            return False

    def _json_body(self):
        try:
            if hasattr(request, 'get_json_data'):
                data = request.get_json_data()
                if data:
                    return data
        except Exception:  # noqa: BLE001
            pass
        raw = request.httprequest.get_data(as_text=True) or ''
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return request.httprequest.form.to_dict()

    def _already_processed_response(self, source):
        return self._action_or_command_text(source, 'This operation has already been processed.')

    def _action_or_command_text(self, source, text):
        if not text.startswith('#'):
            text = '### Odoo AI\n\n%s' % text
        if source == 'action':
            return self._json_response({'update': {'message': text, 'props': {}}, 'ephemeral_text': text})
        return self._json_response({'response_type': 'ephemeral', 'text': text})

    def _tokens_match(self, incoming, expected):
        incoming = str(incoming or '')
        expected = str(expected or '')
        if len(incoming) != len(expected):
            return False
        try:
            return hmac.compare_digest(incoming, expected)
        except (TypeError, ValueError):
            return False

    def _json_response(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False)
        headers = [('Content-Type', 'application/json; charset=utf-8')]
        return request.make_response(body, headers=headers, status=status)
