# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from odoo import api, fields, models, _
from odoo.tools.misc import format_date
from odoo.osv import expression
from datetime import date, datetime, timedelta
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class ResPartner(models.Model):
    _name = 'res.partner'
    _inherit = 'res.partner'

    payment_next_action_date = fields.Date('Next Action Date', copy=False, company_dependent=True,
                                           help="The date before which no action should be taken.")
    unreconciled_aml_ids = fields.One2many('account.move.line', compute='_compute_unreconciled_aml_ids')

    unpaid_invoices = fields.One2many('account.move', compute='_compute_unpaid_invoices')
    total_due = fields.Monetary(compute='_compute_for_followup')
    total_overdue = fields.Monetary(compute='_compute_for_followup')
    followup_status = fields.Selection(
        [('in_need_of_action', 'In need of action'), ('with_overdue_invoices', 'With overdue invoices'), ('no_action_needed', 'No action needed')],
        compute='_compute_for_followup',
        string='Follow-up Status',
        search='_search_status')
    followup_level = fields.Many2one(
        comodel_name='account_followup.followup.line',
        compute="_compute_for_followup",
        string="Follow-up Level",
        search='_search_followup_level',
    )
    payment_responsible_id = fields.Many2one('res.users', ondelete='set null', string='Follow-up Responsible',
                                             help="Optionally you can assign a user to this field, which will make him responsible for the action.",
                                             tracking=True, copy=False, company_dependent=True)

    def _search_status(self, operator, value):
        """
        Compute the search on the field 'followup_status'
        """
        if isinstance(value, str):
            value = [value]
        value = [v for v in value if v in ['in_need_of_action', 'with_overdue_invoices', 'no_action_needed']]
        if operator not in ('in', '=') or not value:
            return []
        followup_data = self._query_followup_level(all_partners=True)
        return [('id', 'in', [d['partner_id'] for d in followup_data.values() if d['followup_status'] in value])]

    def _search_followup_level(self, operator, value):
        company_domain = [('company_id', '=', self.env.company.id)]
        if isinstance(value, str):
            domain = [('name', operator, value)]
        elif isinstance(value, (int, list, tuple)):
            domain = [('id', operator, value)]

        first_followup_level = self.env['account_followup.followup.line'].search(company_domain, order="delay asc", limit=1)
        level_ids = set(self.env['account_followup.followup.line'].search(domain+company_domain).ids)
        if first_followup_level.id in level_ids:
            # the result from the query is None when it is not  yet at a followup level
            # but it is set to the first level in the compute method
            level_ids.add(None)

        followup_data = self._query_followup_level(all_partners=True)

        return [('id', 'in', [
            d['partner_id']
            for d in followup_data.values()
            if d['followup_level'] in level_ids
        ])]

    @api.depends_context('company', 'allowed_company_ids')
    def _compute_for_followup(self):
        """
        Compute the fields 'total_due', 'total_overdue','followup_level' and 'followup_status'
        """
        first_followup_level = self.env['account_followup.followup.line'].search([('company_id', '=', self.env.company.id)], order="delay asc", limit=1)
        followup_data = self._query_followup_level()
        today = fields.Date.context_today(self)
        for record in self:
            total_due = 0
            total_overdue = 0
            for aml in record.unreconciled_aml_ids:
                if aml.company_id == self.env.company and not aml.blocked:
                    amount = aml.amount_residual
                    total_due += amount
                    is_overdue = today > aml.date_maturity if aml.date_maturity else today > aml.date
                    if is_overdue:
                        total_overdue += amount
            record.total_due = total_due
            record.total_overdue = total_overdue
            if record.id in followup_data:
                record.followup_status = followup_data[record.id]['followup_status']
                record.followup_level = self.env['account_followup.followup.line'].browse(followup_data[record.id]['followup_level']) or first_followup_level
            else:
                record.followup_status = 'no_action_needed'
                record.followup_level = first_followup_level

    def _compute_unpaid_invoices(self):
        for record in self:
            record.unpaid_invoices = self.env['account.move'].search([
                ('company_id', '=', self.env.company.id),
                ('commercial_partner_id', '=', record.id),
                ('state', '=', 'posted'),
                ('payment_state', 'in', ('not_paid', 'partial')),
                ('move_type', 'in', self.env['account.move'].get_sale_types())
            ]).filtered(lambda inv: not any(inv.line_ids.mapped('blocked')))

    @api.depends('invoice_ids')
    @api.depends_context('company', 'allowed_company_ids')
    def _compute_unreconciled_aml_ids(self):
        values = {
            read['partner_id'][0]: read['line_ids']
            for read in self.env['account.move.line'].read_group(
                domain=self._get_unreconciled_aml_domain(),
                fields=['line_ids:array_agg(id)'],
                groupby=['partner_id']
            )
        }
        for partner in self:
            partner.unreconciled_aml_ids = values.get(partner.id, False)

    def _get_unreconciled_aml_domain(self):
        return [
            ('reconciled', '=', False),
            ('account_id.deprecated', '=', False),
            ('account_id.internal_type', '=', 'receivable'),
            ('move_id.state', '=', 'posted'),
            ('partner_id', 'in', self.ids),
            ('company_id', '=', self.env.company.id),
        ]

    def get_next_action(self, followup_line):
        """
        Compute the next action status of the customer.
        """
        self.ensure_one()
        date_auto = followup_line._get_next_date()
        return {
            'date': self.payment_next_action_date or date_auto,
        }

    def update_next_action(self, options=False):
        """Updates the next_action_date of the right account move lines"""
        next_action_date = options.get('next_action_date') and options['next_action_date'][0:10] or False
        next_action_date_done = False
        today = date.today()
        fups = self._compute_followup_lines()
        for partner in self:
            if options['action'] == 'done':
                next_action_date_done = datetime.strftime(partner.followup_level._get_next_date(), DEFAULT_SERVER_DATE_FORMAT)
            partner.payment_next_action_date = (not next_action_date or options['action'] == 'done') and next_action_date_done or next_action_date
            if options['action'] in ('done', 'later'):
                msg = _('Next Reminder Date set to %s', format_date(self.env, partner.payment_next_action_date))
                partner.message_post(body=msg)
            if options['action'] == 'done':
                for aml in partner.unreconciled_aml_ids:
                    index = aml.followup_line_id.id or None
                    followup_date = fups[index][0]
                    next_level = fups[index][1]
                    is_overdue = followup_date >= aml.date_maturity if aml.date_maturity else followup_date >= aml.date
                    if is_overdue:
                        aml.write({'followup_line_id': next_level, 'followup_date': today})

    def open_action_followup(self):
        self.ensure_one()
        return {
            'name': _("Overdue Payments for %s", self.display_name),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'views': [[self.env.ref('account_followup.customer_statements_form_view').id, 'form']],
            'res_model': 'res.partner',
            'res_id': self.id,
        }

    def send_followup_email(self):
        """
        Send a follow-up report by email to customers in self
        """
        for record in self:
            options = {
                'partner_id': record.id,
            }
            self.env['account.followup.report'].send_email(options)

    def send_followup_sms(self):
        """
        Send a follow-up report by sms to customers in self
        """
        for partner in self:
            options = {
                'partner_id': partner.id,
            }
            partner._message_sms(
                body=self.env['account.followup.report'].with_context(lang=self.lang or self.env.user.lang)._get_sms_summary(options),
                partner_ids=partner.ids
            )

    def get_followup_html(self):
        """
        Return the content of the follow-up report in HTML
        """
        options = {
            'partner_id': self.id,
            'followup_level': (self.followup_level.id, self.followup_level.delay),
            'keep_summary': True
        }
        return self.env['account.followup.report'].with_context(print_mode=True, lang=self.lang or self.env.user.lang).get_html(options)

    def _compute_followup_lines(self):
        """ returns the followup plan of the current user's company (of given in context directly)
        in the form of a dictionary with
         * keys being the different possible levels of followup for account.move.line's (None or IDs of account_followup.followup.line)
         * values being a tuple of 3 elements corresponding respectively to
           - the oldest date that any line in that followup level should be compared to in order to know if it is ready for the next level
           - the followup ID of the next level
           - the delays in days of the next level
        """
        followup_line_ids = self.env['account_followup.followup.line'].search([('company_id', '=', self.env.company.id)], order="delay asc")

        current_date = fields.Date.today()

        previous_level = None
        fups = {}
        for line in followup_line_ids:
            delay = timedelta(days=line.delay)
            delay_in_days = line.delay
            fups[previous_level] = (current_date - delay, line.id, delay_in_days)
            previous_level = line.id
        if previous_level:
            fups[previous_level] = (current_date - delay, previous_level, delay_in_days)
        return fups

    def _query_followup_level(self, all_partners=False):
        # Allow mocking the current day for testing purpose.
        today = fields.Date.context_today(self)
        if not self.ids and not all_partners:
            return {}

        sql = """
            SELECT partner.id as partner_id,
                   ful.id as followup_level,
                   CASE WHEN partner.balance <= 0 THEN 'no_action_needed'
                        WHEN in_need_of_action_aml.id IS NOT NULL AND (prop_date.value_datetime IS NULL OR prop_date.value_datetime::date <= %(current_date)s) THEN 'in_need_of_action'
                        WHEN exceeded_unreconciled_aml.id IS NOT NULL THEN 'with_overdue_invoices'
                        ELSE 'no_action_needed' END as followup_status
            FROM (
          SELECT partner.id,
                 max(current_followup_level.delay) as followup_delay,
                 SUM(aml.balance) as balance
            FROM res_partner partner
            JOIN account_move_line aml ON aml.partner_id = partner.id
            JOIN account_account account ON account.id = aml.account_id
            JOIN account_move move ON move.id = aml.move_id
            -- Get the followup level
       LEFT JOIN LATERAL (
                         SELECT COALESCE(next_ful.id, ful.id) as id, COALESCE(next_ful.delay, ful.delay) as delay
                           FROM account_move_line line
                LEFT OUTER JOIN account_followup_followup_line ful ON ful.id = aml.followup_line_id
                LEFT OUTER JOIN account_followup_followup_line next_ful ON next_ful.id = (
                    SELECT next_ful.id FROM account_followup_followup_line next_ful
                    WHERE next_ful.delay > COALESCE(ful.delay, -999)
                      AND COALESCE(aml.date_maturity, aml.date) + next_ful.delay <= %(current_date)s
                      AND next_ful.company_id = %(company_id)s
                    ORDER BY next_ful.delay ASC
                    LIMIT 1
                )
                          WHERE line.id = aml.id
                            AND aml.partner_id = partner.id
                            AND aml.balance > 0
            ) current_followup_level ON true
           WHERE account.deprecated IS NOT TRUE
             AND account.internal_type = 'receivable'
             AND move.state = 'posted'
             AND aml.reconciled IS NOT TRUE
             AND aml.blocked IS FALSE
             AND aml.company_id = %(company_id)s
             {where}
        GROUP BY partner.id
            ) partner
            LEFT JOIN account_followup_followup_line ful ON ful.delay = partner.followup_delay AND ful.company_id = %(company_id)s
            -- Get the followup status data
            LEFT OUTER JOIN LATERAL (
                SELECT line.id
                  FROM account_move_line line
                  JOIN account_account account ON line.account_id = account.id
                  JOIN account_move move ON line.move_id = move.id
             LEFT JOIN account_followup_followup_line ful ON ful.id = line.followup_line_id
                 WHERE line.partner_id = partner.id
                   AND account.internal_type = 'receivable'
                   AND account.deprecated IS NOT TRUE
                   AND move.state = 'posted'
                   AND line.reconciled IS NOT TRUE
                   AND line.balance > 0
                   AND line.blocked IS FALSE
                   AND line.company_id = %(company_id)s
                   AND COALESCE(ful.delay, -999) < partner.followup_delay
                   AND COALESCE(line.date_maturity, line.date) + COALESCE(ful.delay, -999) <= %(current_date)s
                 LIMIT 1
            ) in_need_of_action_aml ON true

            LEFT OUTER JOIN LATERAL (
                SELECT line.id
                  FROM account_move_line line
                  JOIN account_account account ON line.account_id = account.id
                  JOIN account_move move ON line.move_id = move.id
                 WHERE line.partner_id = partner.id
                   AND account.internal_type = 'receivable'
                   AND account.deprecated IS NOT TRUE
                   AND move.state = 'posted'
                   AND line.reconciled IS NOT TRUE
                   AND line.balance > 0
                   AND line.blocked IS FALSE
                   AND line.company_id = %(company_id)s
                   AND COALESCE(line.date_maturity, line.date) <= %(current_date)s
                 LIMIT 1
            ) exceeded_unreconciled_aml ON true

            LEFT OUTER JOIN ir_property prop_date ON prop_date.res_id = CONCAT('res.partner,', partner.id)
                                                 AND prop_date.name = 'payment_next_action_date'
                                                 AND prop_date.company_id = %(company_id)s
        """.format(
            where="" if all_partners else "AND aml.partner_id in %(partner_ids)s",
        )
        params = {
            'company_id': self.env.company.id,
            'partner_ids': tuple(self.ids),
            'current_date': today,
        }
        self.env['account.move.line'].flush()
        self.env['res.partner'].flush()
        self.env['account_followup.followup.line'].flush()
        self.env.cr.execute(sql, params)
        result = self.env.cr.dictfetchall()
        result = {r['partner_id']: r for r in result}
        return result

    def _execute_followup_partner(self):
        self.ensure_one()
        if self.followup_status == 'in_need_of_action':
            followup_line = self.followup_level
            if followup_line.send_email:
                self.send_followup_email()
            if followup_line.manual_action:
                # log a next activity for today
                self.activity_schedule(
                    activity_type_id=followup_line.manual_action_type_id and followup_line.manual_action_type_id.id or self._default_activity_type().id,
                    summary=followup_line.manual_action_note,
                    user_id=(followup_line.manual_action_responsible_id and followup_line.manual_action_responsible_id.id) or self.env.user.id
                )
            if followup_line:
                next_date = followup_line._get_next_date()
                self.update_next_action(options={'next_action_date': datetime.strftime(next_date, DEFAULT_SERVER_DATE_FORMAT), 'action': 'done'})
            if followup_line.send_sms:
               self.send_followup_sms()
            if followup_line.print_letter:
                return self
        return None

    def execute_followup(self):
        """
        Execute the actions to do with followups.
        """
        to_print = self.env['res.partner']
        for partner in self:
            partner_tmp = partner._execute_followup_partner()
            if partner_tmp:
                to_print += partner_tmp
        if not to_print:
            return
        return self.env['account.followup.report'].print_followups(to_print)

    def _cron_execute_followup_company(self):
        followup_data = self._query_followup_level(all_partners=True)
        in_need_of_action = self.env['res.partner'].browse([d['partner_id'] for d in followup_data.values() if d['followup_status'] == 'in_need_of_action'])
        in_need_of_action_auto = in_need_of_action.filtered(lambda p: p.followup_level.auto_execute)
        for partner in in_need_of_action_auto:
            try:
                partner._execute_followup_partner()
            except UserError as e:
                # followup may raise exception due to configuration issues
                # i.e. partner missing email
                _logger.exception(e)

    def _cron_execute_followup(self):
        for company in self.env["res.company"].search([]):
            self.with_context(allowed_company_ids=company.ids)._cron_execute_followup_company()
