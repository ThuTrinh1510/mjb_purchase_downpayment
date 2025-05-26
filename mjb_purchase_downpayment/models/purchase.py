from odoo import models, fields, api, _
from odoo.tools.float_utils import float_compare
import logging
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare, float_is_zero, float_round
from itertools import groupby
from odoo.fields import Command

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    amount_to_bill = fields.Monetary(string="Un-billed Balance", compute='_compute_amount_to_invoice')
    amount_billed = fields.Monetary(string="Already billed", compute='_compute_amount_billed')
    
    @api.depends('order_line.amount_to_bill')
    def _compute_amount_to_invoice(self):
        for order in self:
            order.amount_to_bill = sum(order.order_line.mapped('amount_to_bill'))

    @api.depends('order_line.amount_billed')
    def _compute_amount_billed(self):
        for order in self:
            order.amount_billed = sum(order.order_line.mapped('amount_billed'))

    #################################################################################
    # Override Odoo Button, to call Wizard Instead of just going to entries view
    def action_view_purchase_downpayment(self):
        view_id = self.env.ref(
            'mjb_purchase_downpayment.view_purchase_advance_payment_inv'
        ).id
        context = self.env.context.copy()
        context.update({'company_id': self.company_id.id})
        if context.get('active_model') and context.get('active_model') != 'purchase.order':
            context.pop('active_model',None)
            context.pop('active_id',None)
            context.pop('active_ids',None)
        view = {
            'name': _('Down Payment'),
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'purchase.advance.payment.inv',
            'view_id': view_id,
            'type': 'ir.actions.act_window',
            'target': 'new',
            'readonly': True,
            'context': context
        }
        return view

    #################################################################################
    # Do not copy if line is downpayment

    def copy_data(self, default=None):
        if default is None:
            default = {}
        if 'order_line' not in default:
            default['order_line'] = [
                Command.create(line.copy_data()[0])
                for line in self.order_line.filtered(lambda l: not l.mjb_is_downpayment)
            ]
        return super().copy_data(default)

    def _get_invoice_grouping_keys(self):
        return ['company_id', 'partner_id', 'currency_id']

    def _nothing_to_invoice_error(self):
        msg = _("""There is nothing to bill!\n
Reason(s) of this behavior could be:
- You should receive your products before billing them: Click on the "truck" icon (top-right of your screen) and follow instructions.
- You should modify the billing policy of your product: Open the product, go to the "Sales tab" and modify billing policy from "received quantities" to "ordered quantities".
        """)
        return UserError(msg)

    def _get_invoiceable_lines(self, final=False):
        """Return the billable lines for order `self`."""
        down_payment_line_ids = []
        invoiceable_line_ids = []
        pending_section = None
        precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')

        for line in self.order_line:
            if line.display_type == 'line_section':
                # Only bill the section if one of its lines is invoiceable
                pending_section = line
                continue
            if line.display_type != 'line_note' and float_is_zero(line.qty_to_invoice, precision_digits=precision):
                continue
            if line.qty_to_invoice > 0 or (line.qty_to_invoice < 0 and final) or line.display_type == 'line_note':
                if line.mjb_is_downpayment:
                    # Keep down payment lines separately, to put them together
                    # at the end of the bill, in a specific dedicated section.
                    down_payment_line_ids.append(line.id)
                    continue
                if pending_section:
                    invoiceable_line_ids.append(pending_section.id)
                    pending_section = None
                invoiceable_line_ids.append(line.id)

        return self.env['purchase.order.line'].browse(invoiceable_line_ids + down_payment_line_ids)

    def _prepare_down_payment_section_line(self, **optional_values):
        """ Prepare the values to create a new down payment section.

        :param dict optional_values: any parameter that should be added to the returned down payment section
        :return: `account.move.line` creation values
        :rtype: dict
        """
        self.ensure_one()
        context = {'lang': self.partner_id.lang}
        down_payments_section_line = {
            'display_type': 'line_section',
            'name': _("Down Payments"),
            'product_id': False,
            'product_uom_id': False,
            'quantity': 0,
            'discount': 0,
            'price_unit': 0,
            'account_id': False,
            **optional_values
        }
        del context
        return down_payments_section_line

    def _nothing_to_invoice_error_message(self):
        return _(
            "Cannot create an bill. No items are available to bill.\n\n"
            "To resolve this issue, please ensure that:\n"
            "   \u2022 The products have been received before attempting to bill them.\n"
            "   \u2022 The invoicing policy of the product is configured correctly.\n\n"
            "If you want to bill based on ordered quantities instead:\n"
            "   \u2022 For consumable or storable products, open the product, go to the 'General Information' tab and change the 'Invoicing Policy' from 'Delivered Quantities' to 'Ordered Quantities'.\n"
            "   \u2022 For services (and other products), change the 'Invoicing Policy' to 'Prepaid/Fixed Price'.\n"
        )

    def _create_invoices(self, grouped=False, final=False, date=None):
        """ Create bill(s) for the given Purchase Order(s).

        :param bool grouped: if True, invoices are grouped by SO id.
            If False, invoices are grouped by keys returned by :meth:`_get_invoice_grouping_keys`
        :param bool final: if True, refunds will be generated if necessary
        :param date: unused parameter
        :returns: created invoices
        :rtype: `account.move` recordset
        :raises: UserError if one of the orders has no invoiceable lines.
        """
        if not self.env['account.move'].check_access_rights('create', False):
            try:
                self.check_access_rights('write')
                self.check_access_rule('write')
            except AccessError:
                return self.env['account.move']

        # 1) Create invoices.
        bill_vals_list = []
        invoice_item_sequence = 0 # Incremental sequencing to keep the lines order on the bill.
        for order in self:
            order = order.with_company(order.company_id).with_context(lang=order.partner_id.lang)

            invoice_vals = order._prepare_invoice()
            invoiceable_lines = order._get_invoiceable_lines(final)

            if not any(not line.display_type for line in invoiceable_lines):
                continue

            invoice_line_vals = []
            down_payment_section_added = False
            for line in invoiceable_lines:
                if not down_payment_section_added and line.mjb_is_downpayment:
                    # Create a dedicated section for the down payments
                    # (put at the end of the invoiceable_lines)
                    invoice_line_vals.append(
                        Command.create(
                            order._prepare_down_payment_section_line(sequence=invoice_item_sequence)
                        ),
                    )
                    down_payment_section_added = True
                    invoice_item_sequence += 1
                invoice_line_vals.append(
                    Command.create(
                        line._prepare_account_move_line()
                    ),
                )
                invoice_item_sequence += 1

            invoice_vals['invoice_line_ids'] += invoice_line_vals
            bill_vals_list.append(invoice_vals)

        if not bill_vals_list and self._context.get('raise_if_nothing_to_invoice', True):
            raise UserError(self._nothing_to_invoice_error_message())

        # 2) Manage 'grouped' parameter: group by (partner_id, currency_id).
        if not grouped:
            new_bill_vals_list = []
            invoice_grouping_keys = self._get_invoice_grouping_keys()
            bill_vals_list = sorted(
                bill_vals_list,
                key=lambda x: [
                    x.get(grouping_key) for grouping_key in invoice_grouping_keys
                ]
            )
            for _grouping_keys, invoices in groupby(bill_vals_list, key=lambda x: [x.get(grouping_key) for grouping_key in invoice_grouping_keys]):
                origins = set()
                payment_refs = set()
                refs = set()
                ref_invoice_vals = None
                for invoice_vals in invoices:
                    if not ref_invoice_vals:
                        ref_invoice_vals = invoice_vals
                    else:
                        ref_invoice_vals['invoice_line_ids'] += invoice_vals['invoice_line_ids']
                    origins.add(invoice_vals['invoice_origin'])
                    payment_refs.add(invoice_vals['payment_reference'])
                    refs.add(invoice_vals['ref'])
                ref_invoice_vals.update({
                    'ref': ', '.join(refs)[:2000],
                    'invoice_origin': ', '.join(origins),
                    'payment_reference': len(payment_refs) == 1 and payment_refs.pop() or False,
                })
                new_bill_vals_list.append(ref_invoice_vals)
            bill_vals_list = new_bill_vals_list

        # 3) Create invoices.

        # As part of the bill creation, we make sure the sequence of multiple PO do not interfere
        # in a single bill. Example:
        # PO 1:
        # - Section A (sequence: 10)
        # - Product A (sequence: 11)
        # PO 2:
        # - Section B (sequence: 10)
        # - Product B (sequence: 11)
        #
        # If PO 1 & 2 are grouped in the same bill, the result will be:
        # - Section A (sequence: 10)
        # - Section B (sequence: 10)
        # - Product A (sequence: 11)
        # - Product B (sequence: 11)
        #
        # Resequencing should be safe, however we resequence only if there are less invoices than
        # orders, meaning a grouping might have been done. This could also mean that only a part
        # of the selected PO are billable, but resequencing in this case shouldn't be an issue.
        if len(bill_vals_list) < len(self):
            PurchaseOrderLine = self.env['purchase.order.line']
            for bill in bill_vals_list:
                sequence = 1
                for line in bill['invoice_line_ids']:
                    line[2]['sequence'] = PurchaseOrderLine._get_invoice_line_sequence(new=sequence, old=line[2]['sequence'])
                    sequence += 1

        # Manage the creation of invoices in sudo because a salesperson must be able to generate an bill from a
        # purchase order without "billing" access rights. However, he should not be able to create an bill from scratch.
        moves = self.env['account.move'].sudo().with_context(default_move_type='in_invoice').create(bill_vals_list)

        # 4) Some moves might actually be refunds: convert them if the total amount is negative
        # We do this after the moves have been created since we need taxes, etc. to know if the total
        # is actually negative or not
        if final:
            moves.sudo().filtered(lambda m: m.amount_total < 0).action_switch_move_type()
        for move in moves:
            if final:
                # Downpayment might have been determined by a fixed amount set by the user.
                # This amount is tax included. This can lead to rounding issues.
                # E.g. a user wants a 100€ DP on a product with 21% tax.
                # 100 / 1.21 = 82.64, 82.64 * 1,21 = 99.99
                # This is already corrected by adding/removing the missing cents on the DP bill,
                # but must also be accounted for on the final bill.

                delta_amount = 0
                for order_line in self.order_line:
                    if not order_line.mjb_is_downpayment:
                        continue
                    inv_amt = order_amt = 0
                    for invoice_line in order_line.invoice_lines:
                        sign = 1 if invoice_line.move_id.is_inbound() else -1
                        if invoice_line.move_id == move:
                            inv_amt += invoice_line.price_total * sign
                        elif invoice_line.move_id.state != 'cancel':  # filter out canceled dp lines
                            order_amt += invoice_line.price_total * sign
                    if inv_amt and order_amt:
                        # if not inv_amt, this order line is not related to current move
                        # if no order_amt, dp order line was not invoiced
                        delta_amount += inv_amt + order_amt

                if not move.currency_id.is_zero(delta_amount):
                    receivable_line = move.line_ids.filtered(
                        lambda aml: aml.account_id.account_type == 'asset_receivable')[:1]
                    product_lines = move.line_ids.filtered(
                        lambda aml: aml.display_type == 'product' and aml.mjb_is_downpayment)
                    tax_lines = move.line_ids.filtered(
                        lambda aml: aml.tax_line_id.amount_type not in (False, 'fixed'))
                    if tax_lines and product_lines and receivable_line:
                        line_commands = [Command.update(receivable_line.id, {
                            'amount_currency': receivable_line.amount_currency + delta_amount,
                        })]
                        delta_sign = 1 if delta_amount > 0 else -1
                        for lines, attr, sign in (
                            (product_lines, 'price_total', -1 if move.is_inbound() else 1),
                            (tax_lines, 'amount_currency', 1),
                        ):
                            remaining = delta_amount
                            lines_len = len(lines)
                            for line in lines:
                                if move.currency_id.compare_amounts(remaining, 0) != delta_sign:
                                    break
                                amt = delta_sign * max(
                                    move.currency_id.rounding,
                                    abs(move.currency_id.round(remaining / lines_len)),
                                )
                                remaining -= amt
                                line_commands.append(Command.update(line.id, {attr: line[attr] + amt * sign}))
                        move.line_ids = line_commands

            move.message_post_with_source(
                'mail.message_origin_link',
                render_values={'self': move, 'origin': move.line_ids.purchase_line_id.order_id},
                subtype_xmlid='mail.mt_note',
            )
        return moves


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    mjb_is_downpayment = fields.Boolean(string='Is Deposit Line')

    amount_to_bill = fields.Monetary(
        string="Un-billed Balance",
        compute='_compute_amount_to_invoice'
    )

    amount_billed = fields.Monetary(
        string="Billed Amount",
        compute='_compute_amount_billed'
    )

    @api.depends('invoice_lines', 'invoice_lines.price_total', 'invoice_lines.move_id.state')
    def _compute_amount_billed(self):
        for line in self:
            amount_billed = 0.0
            for invoice_line in line._get_invoice_lines():
                bill = invoice_line.move_id
                if bill.state == 'posted':
                    bill_date = bill.invoice_date or fields.Date.context_today(self)
                    # Convert the price_total to the currency of the purchase order line
                    amount_billed_unsigned = invoice_line.currency_id._convert(
                        invoice_line.price_total, line.currency_id, line.company_id, bill_date
                    )
                    # Handle direction sign for vendor bills and refunds
                    if bill.move_type == 'in_invoice':
                        amount_billed += amount_billed_unsigned
                    elif bill.move_type == 'in_refund':
                        amount_billed -= amount_billed_unsigned
            line.amount_billed = amount_billed

    @api.depends('price_total', 'amount_billed')
    def _compute_amount_to_invoice(self):
        for line in self:
            # Calculate the amount that is yet to be billed
            line.amount_to_bill = line.price_total - line.amount_billed