# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tools import format_date, frozendict


class purchaseAdvancePaymentInv(models.TransientModel):
    _name = 'purchase.advance.payment.inv'
    _description = "purchases Advance Payment Bill"
    
    @api.model
    def _default_deposit_account_id(self):
        return self.product_id._get_product_accounts()['expense']

    @api.model
    def _default_deposit_taxes_id(self):
        return self.product_id.supplier_taxes_id

    advance_payment_method = fields.Selection(
        selection=[
            ('delivered', "Regular Bill"),
            ('percentage', "Down payment (percentage)"),
            ('fixed', "Down payment (fixed amount)"),
        ],
        string="Create Bill",
        default='delivered',
        required=True,
        help="A standard Bill is issued with all the order lines ready for invoicing,"
            "according to their invoicing policy (based on ordered or delivered quantity).")
    count = fields.Integer(string="Order Count", compute='_compute_count')
    purchase_order_ids = fields.Many2many(
        'purchase.order', default=lambda self: self.env.context.get('active_ids'))

    # Down Payment logic
    has_down_payments = fields.Boolean(
        string="Has down payments", compute="_compute_has_down_payments")
    deduct_down_payments = fields.Boolean(string="Deduct down payments", default=True)

    # New Down Payment
    product_id = fields.Many2one(
        comodel_name='product.product',
        string="Down Payment Product",
        domain=[('type', '=', 'service')],
        compute='_compute_product_id',
        readonly=False,
        store=True)
    amount = fields.Float(
        string="Down Payment Amount",
        help="The percentage of amount to be billed in advance.")
    fixed_amount = fields.Monetary(
        string="Down Payment Amount (Fixed)",
        help="The fixed amount to be billed in advance.")
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        compute='_compute_currency_id',
        store=True)
    company_id = fields.Many2one(
        comodel_name='res.company',
        compute='_compute_company_id',
        store=True)
    amount_billed = fields.Monetary(
        string="Already billed",
        compute="_compute_bill_amounts",
        help="Only confirmed down payments are considered.")
    amount_to_bill = fields.Monetary(
        string="Amount to bill",
        compute="_compute_bill_amounts",
        help="The amount to bill = Purchase Order Total - Confirmed Down Payments.")

    # Only used when there is no down payment product available
    #  to setup the down payment product
    deposit_account_id = fields.Many2one(
        comodel_name='account.account',
        string="Income Account",
        domain=[('deprecated', '=', False)],
        check_company=True,
        help="Account used for deposits",
        default=_default_deposit_account_id)

    deposit_taxes_id = fields.Many2many(
        comodel_name='account.tax',
        string="Customer Taxes",
        domain=[('type_tax_use', '=', 'purchase')],
        check_company=True,
        help="Taxes used for deposits",
        default=_default_deposit_taxes_id)

    # UI
    display_draft_bill_warning = fields.Boolean(compute="_compute_display_draft_bill_warning")
    display_bill_amount_warning = fields.Boolean(compute="_compute_display_bill_amount_warning")
    consolidated_billing = fields.Boolean(
        string="Consolidated Billing", default=True,
        help="Create one bill for all orders related to same customer and same invoicing address"
    )

    #=== COMPUTE METHODS ===#

    @api.depends('purchase_order_ids')
    def _compute_count(self):
        for wizard in self:
            wizard.count = len(wizard.purchase_order_ids)

    @api.depends('purchase_order_ids')
    def _compute_has_down_payments(self):
        for wizard in self:
            wizard.has_down_payments = bool(
                wizard.purchase_order_ids.order_line.filtered('mjb_is_downpayment')
            )

    # next computed fields are only used for down payments bills and therefore should only
    # have a value when 1 unique PO is billed through the wizard
    @api.depends('purchase_order_ids')
    def _compute_currency_id(self):
        self.currency_id = False
        for wizard in self:
            if wizard.count == 1:
                wizard.currency_id = wizard.purchase_order_ids.currency_id

    @api.depends('purchase_order_ids')
    def _compute_company_id(self):
        self.company_id = False
        for wizard in self:
            if wizard.count == 1:
                wizard.company_id = wizard.purchase_order_ids.company_id

    @api.depends('company_id')
    def _compute_product_id(self):
        self.product_id = False
        for wizard in self:
            if wizard.count == 1:
                product_id = self.env['ir.config_parameter'].get_param("mjb_purchase_downpayment.mjb_deposit_product_id",False)
                if not product_id:
                    product =  wizard.company_id.purchase_down_payment_product_id
                else:
                    product = self.env['product.product'].browse(int(product_id))
                wizard.product_id = product

    @api.depends('amount', 'fixed_amount', 'advance_payment_method', 'amount_to_bill')
    def _compute_display_bill_amount_warning(self):
        for wizard in self:
            bill_amount = wizard.fixed_amount
            if wizard.advance_payment_method == 'percentage':
                bill_amount = wizard.amount / 100 * sum(wizard.purchase_order_ids.mapped('amount_total'))
            wizard.display_bill_amount_warning = bill_amount > wizard.amount_to_bill

    @api.depends('purchase_order_ids')
    def _compute_display_draft_bill_warning(self):
        for wizard in self:
            wizard.display_draft_bill_warning = wizard.purchase_order_ids.invoice_ids.filtered(lambda bill: bill.state == 'draft')

    @api.depends('purchase_order_ids')
    def _compute_bill_amounts(self):
        for wizard in self:
            wizard.amount_billed = sum(wizard.purchase_order_ids._origin.mapped('amount_billed'))
            wizard.amount_to_bill = sum(wizard.purchase_order_ids._origin.mapped('amount_to_bill'))

    #=== ONCHANGE METHODS ===#

    @api.onchange('advance_payment_method')
    def _onchange_advance_payment_method(self):
        if self.advance_payment_method == 'percentage':
            amount = self.default_get(['amount']).get('amount')
            return {'value': {'amount': amount}}

    #=== CONSTRAINT METHODS ===#

    def _check_amount_is_positive(self):
        for wizard in self:
            if wizard.advance_payment_method == 'percentage' and wizard.amount <= 0.00:
                raise UserError(_('The value of the down payment amount must be positive.'))
            elif wizard.advance_payment_method == 'fixed' and wizard.fixed_amount <= 0.00:
                raise UserError(_('The value of the down payment amount must be positive.'))

    @api.constrains('product_id')
    def _check_down_payment_product_is_valid(self):
        for wizard in self:
            if wizard.count > 1 or not wizard.product_id or wizard.advance_payment_method == 'delivered':
                continue
            if wizard.product_id.purchase_method != 'purchase':
                raise UserError(_(
                    "The product used to bill a down payment should have an bill policy"
                    "set to \"Ordered quantities\"."
                    " Please update your deposit product to be able to create a deposit bill."))
            if wizard.product_id.type != 'service':
                raise UserError(_(
                    "The product used to bill a down payment should be of type 'Service'."
                    " Please use another product or update this product."))

    #=== ACTION METHODS ===#

    def create_invoices(self):
        self._check_amount_is_positive()
        bills = self._create_invoices(self.purchase_order_ids)
        return self.purchase_order_ids.action_view_invoice(bills)

    def view_draft_bills(self):
        return {
            'name': _('Draft Bills'),
            'type': 'ir.actions.act_window',
            'view_mode': 'tree',
            'views': [(False, 'list'), (False, 'form')],
            'res_model': 'account.move',
            'domain': [('line_ids.purchase_line_ids.order_id', 'in', self.purchase_order_ids.ids), ('state', '=', 'draft')],
        }

    #=== BUSINESS METHODS ===#

    def _create_invoices(self, purchase_orders):
        self.ensure_one()
        if self.advance_payment_method == 'delivered':
            return purchase_orders._create_invoices(final=self.deduct_down_payments, grouped=not self.consolidated_billing)
        else:
            self.purchase_order_ids.ensure_one()
            self = self.with_company(self.company_id)
            order = self.purchase_order_ids

            # Create deposit product if necessary
            if not self.product_id:
                self.company_id.sudo().purchase_down_payment_product_id = self.env['product.product'].create(
                    self._prepare_down_payment_product_values()
                )
                self._compute_product_id()

            # Create down payment section if necessary
            purchaseOrderline = self.env['purchase.order.line'].with_context(purchase_no_log_for_new_lines=True)
            if not any(line.display_type and line.mjb_is_downpayment for line in order.order_line):
                purchaseOrderline.create(
                    self._prepare_down_payment_section_values(order)
                )

            down_payment_lines = purchaseOrderline.create(
                self._prepare_down_payment_lines_values(order)
            )

            bill = self.env['account.move'].sudo().create(
                self._prepare_invoice_values(order, down_payment_lines)
            )

            # Ensure the bill total is exactly the expected fixed amount.
            if self.advance_payment_method == 'fixed':
                delta_amount = (bill.amount_total - self.fixed_amount) * (1 if bill.is_inbound() else -1)
                if not order.currency_id.is_zero(delta_amount):
                    receivable_line = bill.line_ids\
                        .filtered(lambda aml: aml.account_id.account_type == 'liability_payable')[:1]
                    product_lines = bill.line_ids\
                        .filtered(lambda aml: aml.display_type == 'product')
                    tax_lines = bill.line_ids\
                        .filtered(lambda aml: aml.tax_line_id.amount_type not in (False, 'fixed'))

                    if product_lines and tax_lines and receivable_line:
                        line_commands = [Command.update(receivable_line.id, {
                            'amount_currency': receivable_line.amount_currency + delta_amount,
                        })]
                        delta_sign = 1 if delta_amount > 0 else -1
                        for lines, attr, sign in (
                            (product_lines, 'price_total', -1),
                            (tax_lines, 'amount_currency', 1),
                        ):
                            remaining = delta_amount
                            lines_len = len(lines)
                            for line in lines:
                                if order.currency_id.compare_amounts(remaining, 0) != delta_sign:
                                    break
                                amt = delta_sign * max(
                                    order.currency_id.rounding,
                                    abs(order.currency_id.round(remaining / lines_len)),
                                )
                                remaining -= amt
                                line_commands.append(Command.update(line.id, {attr: line[attr] + amt * sign}))
                        bill.line_ids = line_commands

            # Unsudo the bill after creation if not already sudoed
            bill = bill.sudo(self.env.su)

            poster = self.env.user._is_internal() and self.env.user.id or SUPERUSER_ID
            bill.with_user(poster).message_post_with_source(
                'mail.message_origin_link',
                render_values={'self': bill, 'origin': order},
                subtype_xmlid='mail.mt_note',
            )

            title = _("Down payment bill")
            order.with_user(poster).message_post(
                body=_("%s has been created", bill._get_html_link(title=title)),
            )

            return bill

    def _prepare_down_payment_product_values(self):
        self.ensure_one()
        return {
            'name': _('Down payment'),
            'product_qty': 0.0,
            'type': 'service',
            'purchase_method': 'purchase',
            'company_id': self.company_id.id,
            'property_account_income_id': self.deposit_account_id.id,
            'taxes_id': [Command.set(self.deposit_taxes_id.ids)],
            'is_downpayment': True,
        }

    def _prepare_down_payment_section_values(self, order):
        context = {'lang': order.partner_id.lang}

        po_values = {
            'name': _('Down Payments'),
            'product_qty': 0.0,
            'order_id': order.id,
            'display_type': 'line_section',
            'mjb_is_downpayment': True,
            'sequence': order.order_line and order.order_line[-1].sequence + 1 or 10,
        }

        del context
        return po_values

    def _prepare_down_payment_lines_values(self, order):
        """ Create one down payment line per tax or unique taxes combination.
            Apply the tax(es) to their respective lines.

            :param order: Order for which the down payment lines are created.
            :return:      An array of dicts with the down payment lines values.
        """
        self.ensure_one()

        if self.advance_payment_method == 'percentage':
            percentage = self.amount / 100
        else:
            percentage = self.fixed_amount / order.amount_total if order.amount_total else 1

        order_lines = order.order_line.filtered(lambda l: not l.display_type and not l.mjb_is_downpayment)
        base_downpayment_lines_values = self._prepare_base_downpayment_line_values(order)

        computed_taxes = self.env['account.tax']._compute_taxes([
                    line._convert_to_tax_base_line_dict()
                    for line in order_lines
                ])
        down_payment_values = []
        for line, tax_repartition in computed_taxes['base_lines_to_update']:
            taxes = line['taxes'].flatten_taxes_hierarchy()
            fixed_taxes = taxes.filtered(lambda tax: tax.amount_type == 'fixed')
            down_payment_values.append([
                taxes - fixed_taxes,
                line['analytic_distribution'],
                tax_repartition['price_subtotal']
            ])
            for fixed_tax in fixed_taxes:
                # Fixed taxes cannot be set as taxes on down payments as they always amounts to 100%
                # of the tax amount. Therefore fixed taxes are removed and are replace by a new line
                # with appropriate amount, and non fixed taxes if the fixed tax affected the base of
                # any other non fixed tax.
                if fixed_tax.price_include:
                    continue

                if fixed_tax.include_base_amount:
                    pct_tax = taxes[list(taxes).index(fixed_tax) + 1:]\
                        .filtered(lambda t: t.is_base_affected and t.amount_type != 'fixed')
                else:
                    pct_tax = self.env['account.tax']
                down_payment_values.append([
                    pct_tax,
                    line['analytic_distribution'],
                    line['quantity'] * fixed_tax.amount
                ])

        downpayment_line_map = {}
        for taxes_id, analytic_distribution, price_subtotal in down_payment_values:
            grouping_key = frozendict({
                'taxes_id': tuple(sorted(taxes_id.ids)),
                'analytic_distribution': analytic_distribution,
            })
            downpayment_line_map.setdefault(grouping_key, {
                **base_downpayment_lines_values,
                **grouping_key,
                'product_qty': 0.0,
                'price_unit': 0.0,
            })
            downpayment_line_map[grouping_key]['price_unit'] += price_subtotal
        for key in downpayment_line_map:
            downpayment_line_map[key]['price_unit'] = \
                order.currency_id.round(downpayment_line_map[key]['price_unit'] * percentage)


        return list(downpayment_line_map.values())

    def _prepare_base_downpayment_line_values(self, order):
        self.ensure_one()
        context = {'lang': order.partner_id.lang}
        po_values = {
            'name': _(
                'Down Payment: %(date)s (Draft)', date=format_date(self.env, fields.Date.today())
            ),
            'product_qty': 0.0,
            'order_id': order.id,
            'discount': 0.0,
            'product_id': self.product_id.id,
            'mjb_is_downpayment': True,
            'sequence': order.order_line and order.order_line[-1].sequence + 1 or 10,
        }
        del context
        return po_values

    def _prepare_invoice_values(self, order, po_lines):
        self.ensure_one()
        return {
            **order._prepare_invoice(),
            'invoice_line_ids': [
                Command.create({
                    **line._prepare_account_move_line(),
                    'quantity': 1.0  # Set the default quantity to 1
                })
                for line in po_lines
            ],
        }

    def _get_down_payment_description(self, order):
        self.ensure_one()
        context = {'lang': order.partner_id.lang}
        if self.advance_payment_method == 'percentage':
            name = _("Down payment of %s%%", self.amount)
        else:
            name = _('Down Payment')
        del context
        return name
