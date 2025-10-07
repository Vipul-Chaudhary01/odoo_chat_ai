# -*- coding: utf-8 -*-
import json
import logging
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError

from .mattermost_ai_tools import READ_TOOLS, WRITE_TOOLS


class MattermostAIRequest(models.Model):
    _name = 'mattermost.ai.request'
    _description = 'Mattermost AI request lifecycle'
    _order = 'create_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(required=True, index=True, copy=False, readonly=True)
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('proposed', 'Proposed'),
            ('confirmed', 'Confirmed'),
            ('executing', 'Executing'),
            ('done', 'Done'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
            ('expired', 'Expired'),
        ],
        default='draft',
        index=True,
        required=True,
    )

    mattermost_user_id = fields.Char(required=True, index=True)
    mattermost_username = fields.Char()
    team_id = fields.Char()
    channel_id = fields.Char()
    channel_name = fields.Char()
    command = fields.Char()

    odoo_user_id = fields.Many2one('res.users', index=True)
    mapping_id = fields.Many2one('mattermost.ai.user.mapping', ondelete='set null')

    prompt = fields.Text(required=True)
    ai_proposal_text = fields.Text()
    proposal_json = fields.Text()
    user_message = fields.Text()
    summary = fields.Char()

    operation_type = fields.Selection(
        selection=[
            ('search', 'Search'),
            ('create', 'Create'),
            ('update', 'Update'),
            ('action', 'Action'),
            ('clarify', 'Clarify'),
        ],
    )
    tool_name = fields.Char()
    target_model = fields.Char()
    target_record_id = fields.Integer()
    tool_arguments = fields.Text()
    requires_confirmation = fields.Boolean(default=True)

    confirmed_by_mm_user_id = fields.Char()
    confirmed_at = fields.Datetime()
    executed_at = fields.Datetime()
    expires_at = fields.Datetime(index=True)
    completed_at = fields.Datetime()

    result_text = fields.Text()
    result_json = fields.Text()
    error_message = fields.Text()
    claude_input_tokens = fields.Integer()
    claude_output_tokens = fields.Integer()

    @api.model
    def _generate_name(self):
        return 'MM-AI-%s' % uuid.uuid4().hex[:8].upper()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault('name', self._generate_name())
        return super().create(vals_list)

    def _is_expired(self):
        self.ensure_one()
        return bool(self.expires_at and self.expires_at < fields.Datetime.now())

    def _claim_for_execution(self):
        """Lock the row and move proposed → executing. Returns False if already processed."""
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id, state FROM mattermost_ai_request WHERE id = %s FOR UPDATE',
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            return False
        self.invalidate_recordset(['state'])
        if self._is_expired():
            if self.state in ('draft', 'proposed', 'confirmed'):
                self.write({'state': 'expired', 'completed_at': fields.Datetime.now()})
            return False
        if self.state != 'proposed':
            return False
        self.write({
            'state': 'executing',
            'confirmed_at': fields.Datetime.now(),
        })
        self.flush_recordset()
        return True

    def execute_stored_proposal(self, user_env):
        """Run the stored tool arguments as the mapped Odoo user. Never trust button payloads."""
        self.ensure_one()
        try:
            arguments = json.loads(self.tool_arguments or '{}')
        except json.JSONDecodeError as exc:
            raise UserError('Stored operation is invalid.') from exc
        tool_name = self.tool_name
        if tool_name not in READ_TOOLS | WRITE_TOOLS:
            raise UserError('Unsupported tool.')
        return user_env['mattermost.ai.tools'].execute_tool(tool_name, arguments)
