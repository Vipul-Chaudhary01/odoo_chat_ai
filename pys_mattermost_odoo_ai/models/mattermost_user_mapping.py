# -*- coding: utf-8 -*-
from odoo import fields, models


class MattermostAIUserMapping(models.Model):
    _name = 'mattermost.ai.user.mapping'
    _description = 'Mattermost user to Odoo user mapping'
    _rec_name = 'mattermost_username'
    _order = 'active desc, mattermost_username asc'

    mattermost_user_id = fields.Char(string='Mattermost User ID', required=True, index=True)
    mattermost_username = fields.Char(string='Mattermost Username')
    odoo_user_id = fields.Many2one('res.users', string='Odoo User', required=True, ondelete='restrict')
    active = fields.Boolean(default=True, index=True)
    allowed_execute = fields.Boolean(string='Allowed to Execute', default=True)
    notes = fields.Text()

    _mm_user_unique = models.Constraint(
        'unique(mattermost_user_id)',
        'Each Mattermost user can be mapped only once.',
    )
