# -*- coding: utf-8 -*-
{
    'name': 'PySquad Mattermost → Odoo AI Operations',
    'version': '19.0.1.4.0',
    'category': 'Productivity',
    'summary': 'Run safe Odoo operations from Mattermost with Claude, confirmation, document import, and audit logging.',
    'description': """
Mattermost → Odoo AI Operations
===============================

Employees send natural-language commands in Mattermost (``/odoo ...``).
Odoo validates the request, Claude proposes a structured operation, write
operations require confirmation, then the operation is executed with the
mapped Odoo user's access rights.

Document import: upload a bill PDF/image in Mattermost, then
``/odoo import bill`` to extract and create a draft vendor bill.

This module is self-contained and does not require ``ps_claude_ai``.
    """,
    'author': 'PySquad Informatics LLP',
    'website': 'https://pysquad.com/odoo',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'base_setup',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_config_parameter.xml',
        'views/mattermost_config_views.xml',
        'views/mattermost_user_mapping_views.xml',
        'views/mattermost_ai_request_views.xml',
        'wizard/mattermost_document_wizard_views.xml',
        'views/menus.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': True,
    'auto_install': False,
}
