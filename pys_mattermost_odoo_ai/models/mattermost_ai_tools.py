# -*- coding: utf-8 -*-
import logging

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

# Installed Apps (technical module name) → Mattermost AI allowlisted models.
# When an app is installed/uninstalled, allowed models update automatically.
MODULE_MODEL_MAP = (
    ('base', ('res.partner',)),
    ('product', ('product.template', 'product.product')),
    ('sale', ('sale.order', 'sale.order.line')),
    ('purchase', ('purchase.order', 'purchase.order.line')),
    ('crm', ('crm.lead',)),
    ('account', ('account.move',)),
)

# Installed Apps → allowlisted workflow methods on those models.
MODULE_ACTION_MAP = (
    ('sale', ('action_confirm', 'action_cancel', 'action_draft', '_create_invoices')),
    ('purchase', ('action_confirm', 'action_cancel', 'button_cancel', 'button_draft')),
    ('account', ('action_post', 'button_draft', 'button_cancel')),
)

# Backward-compatible export used by settings/imports.
DEFAULT_ALLOWED_MODELS = tuple(
    model
    for _module, models_ in MODULE_MODEL_MAP
    for model in models_
)
DEFAULT_ALLOWED_ACTIONS = (
    'action_confirm',
    'action_cancel',
    'action_draft',
    'action_post',
    'button_draft',
    'button_cancel',
    '_create_invoices',
)

BLOCKED_MODELS = {
    'ir.config_parameter', 'ir.model', 'ir.model.fields', 'ir.model.access',
    'ir.rule', 'ir.module.module', 'ir.attachment', 'res.users', 'res.groups',
    'res.users.apikeys', 'res.users.apikeys.description',
}
BLOCKED_PREFIXES = ('ir.', 'base.', 'bus.')

# Claude often invents UI/wizard method names; map them to the real SO API.
ACTION_METHOD_ALIASES = {
    'create_invoices': '_create_invoices',
    'action_invoice_create': '_create_invoices',
    'action_create_invoice': '_create_invoices',
    'invoice_create': '_create_invoices',
    'create_invoice': '_create_invoices',
}

MAX_SEARCH_LIMIT = 20
MAX_DOMAIN_LENGTH = 20
MAX_FIELDS = 40

WRITE_TOOLS = {'create_record', 'write_record', 'execute_action'}
READ_TOOLS = {'search_records', 'get_record'}


class MattermostAITools(models.AbstractModel):
    """Allowlisted ORM operations executed as the mapped Odoo user."""

    _name = 'mattermost.ai.tools'
    _description = 'Mattermost AI Odoo Tools'

    @api.model
    def _installed_module_names(self):
        return set(
            self.env['ir.module.module'].sudo().search([
                ('state', '=', 'installed'),
            ]).mapped('name')
        )

    @api.model
    def _is_model_blocked(self, model_name):
        return (
            not model_name
            or model_name in BLOCKED_MODELS
            or any(model_name.startswith(prefix) for prefix in BLOCKED_PREFIXES)
        )

    @api.model
    def _get_models_from_installed_apps(self):
        """Return allowlisted models for currently installed Apps."""
        installed = self._installed_module_names()
        models_out = []
        for module_name, model_names in MODULE_MODEL_MAP:
            if module_name != 'base' and module_name not in installed:
                continue
            for model_name in model_names:
                if self._is_model_blocked(model_name):
                    continue
                if model_name not in self.env or model_name in models_out:
                    continue
                models_out.append(model_name)
        return models_out

    @api.model
    def _get_app_model_coverage(self):
        """Human-readable mapping for Configuration screen."""
        installed = self._installed_module_names()
        rows = []
        for module_name, model_names in MODULE_MODEL_MAP:
            active = module_name == 'base' or module_name in installed
            available = [m for m in model_names if m in self.env and not self._is_model_blocked(m)]
            rows.append({
                'module': module_name,
                'active': active,
                'models': available,
            })
        return rows

    @api.model
    def _get_allowed_models(self):
        """Auto from installed Apps + optional extra models from Configuration."""
        models_out = self._get_models_from_installed_apps()
        extras_raw = self.env['ir.config_parameter'].sudo().get_param(
            'pys_mm_odoo_ai.extra_models', ''
        ) or ''
        for part in extras_raw.replace('\n', ',').split(','):
            name = part.strip()
            if not name or name in models_out or self._is_model_blocked(name):
                continue
            if name in self.env:
                models_out.append(name)
        return models_out

    @api.model
    def _get_allowed_actions(self):
        """Auto from installed Apps + optional extra actions from Configuration."""
        installed = self._installed_module_names()
        actions = set()
        for module_name, method_names in MODULE_ACTION_MAP:
            if module_name in installed:
                actions.update(method_names)
        extras_raw = self.env['ir.config_parameter'].sudo().get_param(
            'pys_mm_odoo_ai.extra_actions', ''
        ) or ''
        for part in extras_raw.replace('\n', ',').split(','):
            name = part.strip()
            if name:
                actions.add(name)
        # Keep safe core actions even if maps are empty.
        if not actions:
            actions.update(DEFAULT_ALLOWED_ACTIONS)
        return actions

    @api.model
    def _assert_model_allowed(self, model_name):
        if not model_name or model_name not in self._get_allowed_models():
            raise UserError(_('Model "%s" is not allowed for Mattermost AI operations.') % (model_name or ''))
        if model_name not in self.env:
            raise UserError(_('Model "%s" is not installed.') % model_name)

    @api.model
    def _sanitize_domain(self, domain):
        if domain in (None, False, ''):
            return []
        if not isinstance(domain, list):
            raise UserError(_('Domain must be a list.'))
        if len(domain) > MAX_DOMAIN_LENGTH:
            raise UserError(_('Domain is too complex.'))
        for leaf in domain:
            if leaf in ('!', '|', '&'):
                continue
            if not isinstance(leaf, (list, tuple)) or len(leaf) != 3:
                raise UserError(_('Invalid domain leaf: %s') % leaf)
            field_name = leaf[0]
            if not isinstance(field_name, str) or field_name.startswith('_'):
                raise UserError(_('Invalid domain field: %s') % field_name)
        return domain

    @api.model
    def _safe_fields(self, model_name, fields_list=None):
        Model = self.env[model_name]
        preferred = [
            'id', 'name', 'display_name', 'email', 'phone', 'mobile', 'vat',
            'state', 'partner_id', 'user_id', 'amount_total', 'list_price',
            'default_code', 'active', 'company_type', 'is_company',
            'move_type', 'invoice_date', 'ref', 'payment_state',
        ]
        allowed = []
        for fname in preferred:
            if fname in Model._fields:
                allowed.append(fname)
        for fname, field in Model._fields.items():
            if len(allowed) >= MAX_FIELDS:
                break
            if fname in allowed or fname.startswith('_'):
                continue
            if field.type in (
                'char', 'text', 'integer', 'float', 'monetary', 'boolean',
                'selection', 'date', 'datetime', 'many2one',
            ) and not field.compute:
                allowed.append(fname)
        if fields_list:
            selected = [f for f in fields_list if f in allowed]
            allowed = selected or allowed
        if 'id' not in allowed:
            allowed = ['id'] + allowed
        return [f for f in allowed if f in Model._fields]

    @api.model
    def _serialize(self, records, fields_list):
        rows = []
        for rec in records:
            row = {}
            for fname in fields_list:
                if fname not in rec._fields:
                    continue
                value = rec[fname]
                field = rec._fields[fname]
                if field.type == 'many2one':
                    row[fname] = {'id': value.id, 'name': value.display_name} if value else False
                elif field.type in ('one2many', 'many2many'):
                    row[fname] = [{'id': r.id, 'name': r.display_name} for r in value[:20]]
                elif field.type == 'binary':
                    row[fname] = bool(value)
                else:
                    row[fname] = value
            rows.append(row)
        return rows

    @api.model
    def _filter_values(self, model_name, values):
        if not isinstance(values, dict):
            raise UserError(_('values must be an object.'))
        values = self._prepare_values(model_name, values)
        Model = self.env[model_name]
        allowed = set(self._safe_fields(model_name)) - {'id', 'display_name'}
        cleaned = {}
        for key, val in values.items():
            if key == 'order_line' and model_name in ('sale.order', 'purchase.order'):
                cleaned[key] = val
                continue
            if key == 'invoice_line_ids' and model_name == 'account.move':
                cleaned[key] = val
                continue
            if key in allowed and key in Model._fields:
                field = Model._fields[key]
                if field.type == 'many2one' and isinstance(val, dict):
                    cleaned[key] = val.get('id')
                else:
                    cleaned[key] = val
        if not cleaned:
            raise UserError(_('No valid fields to write.'))
        return cleaned

    @api.model
    def _prepare_values(self, model_name, values):
        """Resolve human names to IDs and normalize special create payloads."""
        values = dict(values or {})
        if model_name == 'sale.order':
            return self._prepare_sale_order_values(values)
        if model_name == 'purchase.order':
            return self._prepare_purchase_order_values(values)
        if model_name == 'crm.lead':
            return self._prepare_crm_lead_values(values)
        if model_name == 'account.move':
            return self._prepare_account_move_values(values)
        return self._resolve_many2one_names(model_name, values)

    @api.model
    def _resolve_many2one_names(self, model_name, values):
        Model = self.env[model_name]
        out = dict(values)
        for key, val in list(out.items()):
            if key not in Model._fields:
                continue
            field = Model._fields[key]
            if field.type != 'many2one':
                continue
            resolved = self._resolve_record_id(field.comodel_name, val)
            if resolved:
                out[key] = resolved
        return out

    @api.model
    def _prepare_sale_order_values(self, values):
        partner_id = self._resolve_record_id(
            'res.partner',
            values.get('partner_id') or values.get('partner_name') or values.get('customer'),
        )
        if not partner_id:
            raise UserError(_(
                'Could not find the customer. Create the customer first, then create the quotation.'
            ))

        lines_in = values.get('order_line') or values.get('order_lines') or values.get('lines') or []
        if not isinstance(lines_in, list) or not lines_in:
            raise UserError(_(
                'Could not build order lines. Include an existing product name and quantity.'
            ))

        commands = []
        for line in lines_in:
            line_vals = {}
            if isinstance(line, (list, tuple)) and len(line) >= 3 and isinstance(line[2], dict):
                line_vals = line[2]
            elif isinstance(line, dict):
                line_vals = line
            else:
                continue
            product_id = self._resolve_product_id(line_vals)
            if not product_id:
                raise UserError(_(
                    'Could not find product "%s". Create the product first.'
                ) % (
                    line_vals.get('product_name')
                    or line_vals.get('product')
                    or line_vals.get('name')
                    or line_vals.get('product_id')
                    or ''
                ))
            qty = line_vals.get('product_uom_qty') or line_vals.get('qty') or line_vals.get('quantity') or 1
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                qty = 1.0
            if qty <= 0:
                qty = 1.0
            commands.append((0, 0, {
                'product_id': product_id,
                'product_uom_qty': qty,
            }))

        if not commands:
            raise UserError(_('Could not build order lines. Include an existing product name and quantity.'))

        return {
            'partner_id': partner_id,
            'order_line': commands,
        }

    @api.model
    def _prepare_purchase_order_values(self, values):
        partner_id = self._resolve_record_id(
            'res.partner',
            values.get('partner_id') or values.get('partner_name') or values.get('vendor') or values.get('supplier'),
        )
        if not partner_id:
            raise UserError(_(
                'Could not find the vendor. Create the vendor/contact first, then create the purchase order.'
            ))

        lines_in = values.get('order_line') or values.get('order_lines') or values.get('lines') or []
        if not isinstance(lines_in, list) or not lines_in:
            raise UserError(_(
                'Could not build purchase lines. Include an existing product name and quantity.'
            ))

        commands = []
        for line in lines_in:
            line_vals = {}
            if isinstance(line, (list, tuple)) and len(line) >= 3 and isinstance(line[2], dict):
                line_vals = line[2]
            elif isinstance(line, dict):
                line_vals = line
            else:
                continue
            product_id = self._resolve_product_id(line_vals)
            if not product_id:
                raise UserError(_(
                    'Could not find product "%s". Create the product first.'
                ) % (
                    line_vals.get('product_name')
                    or line_vals.get('product')
                    or line_vals.get('name')
                    or line_vals.get('product_id')
                    or ''
                ))
            qty = (
                line_vals.get('product_qty')
                or line_vals.get('product_uom_qty')
                or line_vals.get('qty')
                or line_vals.get('quantity')
                or 1
            )
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                qty = 1.0
            if qty <= 0:
                qty = 1.0
            commands.append((0, 0, {
                'product_id': product_id,
                'product_qty': qty,
            }))

        if not commands:
            raise UserError(_('Could not build purchase lines. Include an existing product name and quantity.'))

        return {
            'partner_id': partner_id,
            'order_line': commands,
        }

    @api.model
    def _prepare_crm_lead_values(self, values):
        out = {}
        name = values.get('name') or values.get('opportunity') or values.get('subject')
        if not name:
            raise UserError(_('CRM lead/opportunity needs a name/title.'))
        out['name'] = name

        lead_type = (values.get('type') or 'opportunity').strip().lower()
        if lead_type not in ('lead', 'opportunity'):
            lead_type = 'opportunity'
        out['type'] = lead_type

        for src, dest in (
            ('email_from', 'email_from'),
            ('email', 'email_from'),
            ('phone', 'phone'),
            ('contact_name', 'contact_name'),
            ('description', 'description'),
        ):
            if values.get(src) and dest not in out:
                out[dest] = values.get(src)

        partner_id = self._resolve_record_id(
            'res.partner',
            values.get('partner_id') or values.get('partner_name') or values.get('customer'),
        )
        if partner_id:
            out['partner_id'] = partner_id
        return out

    @api.model
    def _prepare_account_move_values(self, values):
        """Create customer invoice or vendor bill from natural-language values."""
        move_type = (values.get('move_type') or values.get('invoice_type') or '').strip().lower()
        type_aliases = {
            'out_invoice': 'out_invoice',
            'customer_invoice': 'out_invoice',
            'customer invoice': 'out_invoice',
            'regular invoice': 'out_invoice',
            'invoice': 'out_invoice',
            'in_invoice': 'in_invoice',
            'vendor_bill': 'in_invoice',
            'vendor bill': 'in_invoice',
            'bill': 'in_invoice',
            'out_refund': 'out_refund',
            'credit_note': 'out_refund',
            'in_refund': 'in_refund',
            'vendor_credit_note': 'in_refund',
        }
        move_type = type_aliases.get(move_type, move_type)
        if move_type not in ('out_invoice', 'in_invoice', 'out_refund', 'in_refund'):
            # Infer from partner role keywords if missing/invalid
            if values.get('vendor') or values.get('supplier'):
                move_type = 'in_invoice'
            else:
                move_type = 'out_invoice'

        partner_id = self._resolve_record_id(
            'res.partner',
            values.get('partner_id')
            or values.get('partner_name')
            or values.get('customer')
            or values.get('vendor')
            or values.get('supplier'),
        )
        if not partner_id:
            raise UserError(_(
                'Could not find the partner for this invoice/bill. Create the contact first.'
            ))

        lines_in = (
            values.get('invoice_line_ids')
            or values.get('invoice_lines')
            or values.get('order_line')
            or values.get('lines')
            or []
        )
        if not isinstance(lines_in, list) or not lines_in:
            raise UserError(_(
                'Could not build invoice lines. Include product name, quantity, and price if needed.'
            ))

        commands = []
        for line in lines_in:
            line_vals = {}
            if isinstance(line, (list, tuple)) and len(line) >= 3 and isinstance(line[2], dict):
                line_vals = line[2]
            elif isinstance(line, dict):
                line_vals = line
            else:
                continue
            product_id = self._resolve_product_id(line_vals)
            qty = line_vals.get('quantity') or line_vals.get('product_uom_qty') or line_vals.get('qty') or 1
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                qty = 1.0
            if qty <= 0:
                qty = 1.0
            cmd = {'quantity': qty}
            if product_id:
                cmd['product_id'] = product_id
            else:
                label = line_vals.get('name') or line_vals.get('product_name') or line_vals.get('label')
                if not label:
                    raise UserError(_('Each invoice line needs a product or a description.'))
                cmd['name'] = label
            price = line_vals.get('price_unit') or line_vals.get('price') or line_vals.get('list_price')
            if price not in (None, False, ''):
                try:
                    cmd['price_unit'] = float(price)
                except (TypeError, ValueError):
                    pass
            commands.append((0, 0, cmd))

        if not commands:
            raise UserError(_('Could not build invoice lines.'))

        out = {
            'move_type': move_type,
            'partner_id': partner_id,
            'invoice_line_ids': commands,
        }
        if values.get('ref') or values.get('payment_reference'):
            out['ref'] = values.get('ref') or values.get('payment_reference')
        if values.get('invoice_date'):
            out['invoice_date'] = values.get('invoice_date')
        return out

    @api.model
    def _resolve_product_id(self, data):
        if not isinstance(data, dict):
            return 0
        raw = data.get('product_id')
        if isinstance(raw, int):
            return raw
        if isinstance(raw, dict):
            return int(raw.get('id') or 0)
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw.strip())
        name = (
            data.get('product_name')
            or data.get('product')
            or data.get('name')
            or (raw if isinstance(raw, str) else None)
        )
        return self._resolve_product_by_name(name)

    @api.model
    def _resolve_product_by_name(self, name):
        if not name or not isinstance(name, str):
            return 0
        name = name.strip()
        if not name:
            return 0
        if 'product.product' in self.env:
            Product = self.env['product.product']
            exact = Product.search([('name', '=', name)], limit=1)
            if exact:
                return exact.id
            recs = Product.search([('name', 'ilike', name)], limit=2)
            if len(recs) == 1:
                return recs.id
        if 'product.template' in self.env:
            Template = self.env['product.template']
            exact = Template.search([('name', '=', name)], limit=1)
            if exact and exact.product_variant_id:
                return exact.product_variant_id.id
            recs = Template.search([('name', 'ilike', name)], limit=2)
            if len(recs) == 1 and recs.product_variant_id:
                return recs.product_variant_id.id
        return 0

    @api.model
    def _resolve_record_id(self, model_name, value):
        if value in (None, False, ''):
            return 0
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, dict):
            return int(value.get('id') or 0)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        if isinstance(value, str) and model_name in self.env:
            Model = self.env[model_name]
            exact = Model.search([('name', '=', value.strip())], limit=1)
            if exact:
                return exact.id
            recs = Model.search([('name', 'ilike', value.strip())], limit=2)
            if len(recs) == 1:
                return recs.id
        return 0

    @api.model
    def execute_tool(self, name, arguments):
        arguments = arguments or {}
        handlers = {
            'search_records': self._tool_search_records,
            'get_record': self._tool_get_record,
            'create_record': self._tool_create_record,
            'write_record': self._tool_write_record,
            'execute_action': self._tool_execute_action,
        }
        handler = handlers.get(name)
        if not handler:
            return {'ok': False, 'error': 'Unknown tool: %s' % name}
        try:
            return handler(arguments)
        except (UserError, AccessError, ValidationError) as exc:
            return {'ok': False, 'error': str(exc), 'access_denied': isinstance(exc, AccessError)}
        except Exception as exc:  # noqa: BLE001
            _logger.exception('Mattermost AI tool %s failed', name)
            return {'ok': False, 'error': 'The requested Odoo operation could not be completed.'}

    @api.model
    def _tool_search_records(self, arguments):
        model_name = arguments.get('model')
        self._assert_model_allowed(model_name)
        domain = self._sanitize_domain(arguments.get('domain') or [])
        limit = max(1, min(int(arguments.get('limit') or 10), MAX_SEARCH_LIMIT))
        fields_list = self._safe_fields(model_name, arguments.get('fields'))
        Model = self.env[model_name]
        Model.check_access('read')
        records = Model.search(domain, limit=limit, order=arguments.get('order') or 'id desc')
        return {
            'ok': True,
            'model': model_name,
            'count': len(records),
            'records': self._serialize(records, fields_list),
        }

    @api.model
    def _tool_get_record(self, arguments):
        model_name = arguments.get('model')
        self._assert_model_allowed(model_name)
        record_id = int(arguments.get('record_id') or 0)
        if record_id <= 0:
            raise UserError(_('record_id must be a positive integer.'))
        fields_list = self._safe_fields(model_name, arguments.get('fields'))
        Model = self.env[model_name]
        Model.check_access('read')
        record = Model.browse(record_id).exists()
        if not record:
            return {'ok': False, 'error': 'Record not found.'}
        record.check_access('read')
        rows = self._serialize(record, fields_list)
        return {'ok': True, 'model': model_name, 'record': rows[0] if rows else {}}

    @api.model
    def _tool_create_record(self, arguments):
        model_name = arguments.get('model')
        self._assert_model_allowed(model_name)
        values = self._filter_values(model_name, arguments.get('values') or {})
        Model = self.env[model_name]
        Model.check_access('create')
        record = Model.create(values)
        record.check_access('read')
        fields_list = self._safe_fields(model_name)
        return {
            'ok': True,
            'created': True,
            'model': model_name,
            'record_id': record.id,
            'record': self._serialize(record, fields_list)[0],
            'message': 'Record created successfully.',
        }

    @api.model
    def _tool_write_record(self, arguments):
        model_name = arguments.get('model')
        self._assert_model_allowed(model_name)
        record_id = int(arguments.get('record_id') or 0)
        if record_id <= 0:
            raise UserError(_('record_id must be a positive integer.'))
        values = self._filter_values(model_name, arguments.get('values') or {})
        Model = self.env[model_name]
        Model.check_access('write')
        record = Model.browse(record_id).exists()
        if not record:
            return {'ok': False, 'error': 'Record not found.'}
        record.check_access('write')
        record.write(values)
        fields_list = self._safe_fields(model_name)
        return {
            'ok': True,
            'updated': True,
            'model': model_name,
            'record_id': record.id,
            'record': self._serialize(record, fields_list)[0],
            'message': 'Record updated successfully.',
        }

    @api.model
    def _tool_execute_action(self, arguments):
        model_name = arguments.get('model')
        self._assert_model_allowed(model_name)
        record_id = int(arguments.get('record_id') or 0)
        method = (arguments.get('method') or '').strip()
        method = ACTION_METHOD_ALIASES.get(method, method)
        if record_id <= 0:
            raise UserError(_('record_id must be a positive integer.'))
        if method not in self._get_allowed_actions():
            raise UserError(_('Method "%s" is not allowed.') % method)
        Model = self.env[model_name]
        if not hasattr(Model, method):
            raise UserError(_('Model %s has no method %s.') % (model_name, method))
        record = Model.browse(record_id).exists()
        if not record:
            return {'ok': False, 'error': 'Record not found.'}
        record.check_access('write')

        if method == '_create_invoices' and model_name == 'sale.order':
            invoices = record._create_invoices(final=True)
            if not invoices:
                raise UserError(_(
                    'No invoiceable lines found on this sales order. '
                    'Confirm the order and ensure products are deliverable/invoiceable.'
                ))
            inv = invoices[0]
            return {
                'ok': True,
                'created': True,
                'model': 'account.move',
                'record_id': inv.id,
                'invoice_ids': invoices.ids,
                'method': method,
                'message': 'Regular invoice created successfully.',
                'record': {
                    'id': inv.id,
                    'name': inv.display_name or inv.name or '/',
                    'state': inv.state,
                    'move_type': inv.move_type,
                    'amount_total': inv.amount_total,
                    'partner_id': {
                        'id': inv.partner_id.id,
                        'name': inv.partner_id.display_name,
                    } if inv.partner_id else False,
                },
            }

        getattr(record, method)()
        payload = {
            'ok': True,
            'model': model_name,
            'record_id': record.id,
            'method': method,
            'message': 'Action %s executed.' % method,
        }
        for fname in ('name', 'state', 'amount_total'):
            if fname in record._fields:
                payload[fname] = record[fname]
        return payload
