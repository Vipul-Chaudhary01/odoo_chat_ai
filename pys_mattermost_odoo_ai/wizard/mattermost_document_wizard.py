# -*- coding: utf-8 -*-
import base64
import json

from odoo import _, fields, models
from odoo.exceptions import UserError
from datetime import timedelta


class MattermostDocumentImportWizard(models.TransientModel):
    _name = 'mattermost.ai.document.wizard'
    _description = 'Import bill/invoice document into Odoo via Claude'

    name = fields.Char(default='Document Import')
    import_kind = fields.Selection(
        selection=[
            ('vendor_bill', 'Vendor Bill'),
            ('customer_invoice', 'Customer Invoice'),
        ],
        default='vendor_bill',
        required=True,
    )
    data_file = fields.Binary(string='PDF / Image', required=True)
    filename = fields.Char()
    mimetype = fields.Char()
    result_preview = fields.Text(readonly=True)
    request_id = fields.Many2one('mattermost.ai.request', readonly=True)

    def action_extract_and_propose(self):
        self.ensure_one()
        if not self.data_file:
            raise UserError(_('Please upload a PDF or image file.'))
        content = base64.b64decode(self.data_file)
        filename = self.filename or 'upload.bin'
        mimetype = (self.mimetype or '').lower()
        if not mimetype:
            if filename.lower().endswith('.pdf'):
                mimetype = 'application/pdf'
            elif filename.lower().endswith('.png'):
                mimetype = 'image/png'
            else:
                mimetype = 'image/jpeg'

        Doc = self.env['mattermost.ai.document.service']
        proposal, raw_text, usage = Doc.extract_bill_proposal(
            {
                'filename': filename,
                'mimetype': mimetype,
                'content': content,
            },
            import_kind=self.import_kind,
        )
        Tools = self.env['mattermost.ai.tools']
        if 'account.move' not in Tools._get_allowed_models():
            raise UserError(_('Accounting (account.move) is not available. Install Invoicing/Accounting app.'))

        values = proposal.get('values') or {}
        values = Tools._prepare_values('account.move', values)
        req = self.env['mattermost.ai.request'].sudo().create({
            'state': 'proposed',
            'mattermost_user_id': 'odoo-wizard',
            'mattermost_username': self.env.user.login,
            'odoo_user_id': self.env.user.id,
            'prompt': 'Import document %s as %s' % (filename, self.import_kind),
            'ai_proposal_text': raw_text,
            'proposal_json': json.dumps(proposal, default=str),
            'tool_name': 'create_record',
            'tool_arguments': json.dumps({'model': 'account.move', 'values': values}, default=str),
            'target_model': 'account.move',
            'operation_type': 'create',
            'requires_confirmation': True,
            'summary': (proposal.get('summary') or 'Document import')[:200],
            'user_message': proposal.get('user_message') or proposal.get('summary'),
            'claude_input_tokens': (usage or {}).get('input_tokens') or 0,
            'claude_output_tokens': (usage or {}).get('output_tokens') or 0,
            'expires_at': fields.Datetime.now() + timedelta(minutes=30),
        })

        preview = (
            'Request: %s\n'
            'Move type: %s\n'
            'Partner: %s\n'
            'Lines: %s\n\n'
            'Open AI Requests and confirm, or run from Mattermost:\n'
            '/odoo confirm %s'
        ) % (
            req.name,
            values.get('move_type'),
            values.get('partner_id'),
            values.get('invoice_line_ids'),
            req.name,
        )
        self.write({
            'result_preview': preview,
            'request_id': req.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mattermost.ai.document.wizard',
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }

    def action_confirm_import(self):
        self.ensure_one()
        if not self.request_id:
            raise UserError(_('Extract the document first.'))
        req = self.request_id
        if not req._claim_for_execution():
            raise UserError(_('This import was already processed or expired.'))
        result = req.execute_stored_proposal(self.env)
        if not result.get('ok'):
            req.write({
                'state': 'failed',
                'error_message': result.get('error') or 'Import failed',
                'completed_at': fields.Datetime.now(),
            })
            raise UserError(result.get('error') or _('Import failed.'))
        req.write({
            'state': 'done',
            'result_json': json.dumps(result, default=str),
            'result_text': result.get('message') or 'Imported',
            'target_record_id': result.get('record_id') or 0,
            'executed_at': fields.Datetime.now(),
            'completed_at': fields.Datetime.now(),
        })
        move = self.env['account.move'].browse(result.get('record_id')).exists()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Imported Document'),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': move.id,
            'target': 'current',
        }
