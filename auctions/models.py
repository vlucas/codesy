import uuid
import requests
import re
import HTMLParser
import stripe
import paypalrestsdk
from paypalrestsdk import Payout as PaypalPayout

from datetime import datetime, timedelta
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.db import models
from django.db.models import Sum
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from decimal import Decimal, ROUND_UP
from mailer import send_mail

from .managers import ClaimManager

stripe.api_key = settings.STRIPE_SECRET_KEY

# TODO: Find a prettier way to authenticate
paypalrestsdk.configure({"mode": settings.PAYPAL_MODE,
                         "client_id": settings.PAYPAL_CLIENT_ID,
                         "client_secret": settings.PAYPAL_CLIENT_SECRET,
                         })

PAYPAL_PAYOUT_RECIPIENT = settings.PAYPAL_PAYOUT_RECIPIENT


def uuid_please():
    full_uuid = uuid.uuid4()
    # uuid is truncated because paypal must be <30
    return str(full_uuid)[:25]


class Bid(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    url = models.URLField()
    title = models.CharField(max_length=255, null=True, blank=True)
    issue = models.ForeignKey('Issue', null=True)
    ask = models.DecimalField(max_digits=6, decimal_places=2, blank=True,
                              default=0)
    ask_match_sent = models.DateTimeField(null=True, blank=True)
    offer = models.DecimalField(max_digits=6, decimal_places=2, blank=True,
                                default=0)
    created = models.DateTimeField(null=True, blank=True)
    modified = models.DateTimeField(null=True, blank=True, auto_now=True)

    class Meta:
        unique_together = (("user", "url"),)

    def __unicode__(self):
        return u'%s bid on %s' % (self.user, self.url)

    def ask_met(self):
        if self.ask:
            other_bids = Bid.objects.filter(
                url=self.url
            ).exclude(
                user=self.user
            ).aggregate(
                Sum('offer')
            )
            return other_bids['offer__sum'] >= self.ask
        else:
            return False

    def offers(self):
        return Offer.objects.filter(bid=self)

    def make_offer(self, offer_amount):
        if not offer_amount:
            return False
        offer_amount = Decimal(offer_amount)
        offers = Offer.objects.filter(bid=self, api_success=True)
        if offers:
            sum_offers = offers.aggregate(Sum('amount'))['amount__sum']
            if offer_amount > sum_offers:
                offer_amount = offer_amount - sum_offers
            else:
                return False
        new_offer = Offer(
            user=self.user,
            amount=offer_amount,
            bid=self,
        )
        new_offer.save()
        return new_offer

    def claim_for_user(self, user):
        return Claim.objects.get(user=user, issue=self.issue)

    def claims_for_other_users(self, user):
        return Claim.objects.filter(issue=self.issue).exclude(user=user)

    def actionable_claims(self, user):
        """
        Returns claims for this bid on which the user can take some action:
            * own_claim: they may request payout
            * other_claims: they may vote
        """
        try:
            own_claim = self.claim_for_user(user)
        except Claim.DoesNotExist:
            own_claim = None

        other_claims = self.claims_for_other_users(user)
        return {'own_claim': own_claim, 'other_claims': other_claims}

    def is_biddable_by(self, user):
        nobid_claim_statuses = ['Submitted', 'Pending', 'Approved', 'Paid']
        if user == self.user and self.ask_met():
            return False
        actionable_claims = self.actionable_claims(user)
        own_claim = actionable_claims['own_claim']
        if own_claim and own_claim.status == 'Rejected':
            return True
        if own_claim and own_claim.status in nobid_claim_statuses:
            return False
        for other_claim in actionable_claims['other_claims']:
            if other_claim.status in nobid_claim_statuses:
                return False
        return True


@receiver(post_save, sender=Bid)
def notify_matching_askers(sender, instance, **kwargs):
    # TODO: make a nicer HTML email template
    ASKER_NOTIFICATION_EMAIL_STRING = """
    Bidders have met your asking price for {url}.
    If you fix the issue, you may claim the payout by visiting the issue url:
    {url}
    """

    unnotified_asks = Bid.objects.filter(
        url=instance.url, ask_match_sent=None).exclude(ask__lte=0)

    for bid in unnotified_asks:
        if bid.ask_met():
            send_mail(
                "[codesy] Your ask for %(ask)d for %(url)s has been met" %
                (
                    {'ask': bid.ask, 'url': bid.url}
                ),
                ASKER_NOTIFICATION_EMAIL_STRING.format(url=bid.url),
                settings.DEFAULT_FROM_EMAIL,
                [bid.user.email]
            )
            # use .update to avoid recursive signal processing
            Bid.objects.filter(id=bid.id).update(ask_match_sent=datetime.now())


@receiver(post_save, sender=Bid)
def create_issue_for_bid(sender, instance, **kwargs):
    issue, created = Issue.objects.get_or_create(
        url=instance.url,
        defaults={'state': 'unknown', 'last_fetched': None}
    )
    # use .update to avoid recursive signal processing
    Bid.objects.filter(id=instance.id).update(issue=issue)


class Issue(models.Model):
    url = models.URLField(unique=True, db_index=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    state = models.CharField(max_length=255)
    last_fetched = models.DateTimeField(auto_now=True)

    def __unicode__(self):
        return u'Issue for %s (%s)' % (self.url, self.state)


class Claim(models.Model):
    STATUS_CHOICES = (
        ('Submitted', 'Submitted'),
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
        ('Paid', 'Paid')
    )
    issue = models.ForeignKey('Issue')
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    created = models.DateTimeField(null=True, blank=True)
    modified = models.DateTimeField(null=True, blank=True, auto_now=True)
    evidence = models.URLField(blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=255,
                              choices=STATUS_CHOICES,
                              default='Submitted')

    objects = ClaimManager()

    class Meta:
        unique_together = (("user", "issue"),)

    def __unicode__(self):
        return u'%s claim on Issue %s (%s)' % (
            self.user, self.issue.id, self.status
        )

    def payouts(self):
        return Payout.objects.filter(claim=self)

    def successful_payouts(self):
        return Payout.objects.filter(claim=self, api_success=True)

    def payout_request(self):
        if self.status == 'Paid':
            return False

        bid = Bid.objects.get(url=self.issue.url, user=self.user)

        payout = Payout(
            user=self.user,
            claim=self,
            amount=bid.ask
        )
        payout.save()

        payout_attempt = payout.request()
        if payout.api_success is True:
            self.status = 'Paid'
            self.save()
        return payout_attempt

    def votes_by_approval(self, approved):
        return (Vote.objects
                    .filter(claim=self, approved=approved)
                    .exclude(user=self.user))

    def needs_vote_from_user(self, user):
        try:
            Vote.objects.get(claim=self, user=user)
            # User has already voted on this claim
            return False
        except Vote.DoesNotExist:
            user_bid = Bid.objects.filter(issue=self.issue,
                                          user=user,
                                          offer__gt=0)
            if user_bid:
                return True
        return False

    @property
    def num_approvals(self):
        return self.votes_by_approval(True).count()

    @property
    def num_rejections(self):
        return self.votes_by_approval(False).count()

    @property
    def num_votes(self):
        return Vote.objects.filter(claim=self).exclude(user=self.user)

    @property
    def offers(self):
        return (Bid.objects.filter(issue=self.issue)
                           .exclude(user=self.user)
                           .filter(offer__gt=0))

    @property
    def expires(self):
        return self.created + timedelta(days=30)

    def get_absolute_url(self):
        return reverse('claim-status', kwargs={'pk': self.id})


@receiver(post_save, sender=Claim)
def notify_matching_offerers(sender, instance, created, **kwargs):
    # Only notify when the claim is first created
    if not created:
        return True

    # TODO: make a nicer HTML email template
    OFFERER_NOTIFICATION_EMAIL_STRING = """
    {user} has claimed the payout for {url}.

    codesy.io will pay your offer of {offer} to {user}.

    To approve or reject this claim, go to:
    https://{site}{claim_link}
    """
    current_site = Site.objects.get_current()

    self_Q = models.Q(user=instance.user)
    offered0_Q = models.Q(offer=0)
    others_bids = Bid.objects.filter(
        issue=instance.issue
    ).exclude(
        self_Q | offered0_Q
    )

    for bid in others_bids:
        send_mail(
            "[codesy] %(user)s has claimed payout for %(url)s" %
            (
                {'user': instance.user, 'url': instance.issue.url}
            ),
            OFFERER_NOTIFICATION_EMAIL_STRING.format(
                user=instance.user,
                url=instance.issue.url,
                offer=bid.offer,
                site=current_site,
                claim_link=instance.get_absolute_url()
            ),
            settings.DEFAULT_FROM_EMAIL,
            [bid.user.email]
        )


@receiver(post_save, sender=Bid)
@receiver(post_save, sender=Issue)
@receiver(post_save, sender=Claim)
def save_title(sender, instance, **kwargs):
    if isinstance(instance, Claim):
        url = instance.evidence
    else:
        url = instance.url

    try:
        r = requests.get(url)
        title_search = re.search('(?:<title.*>)(.*)(?:<\/title>)', r.text)
        if title_search:
            title = HTMLParser.HTMLParser().unescape(title_search.group(1))
            type(instance).objects.filter(id=instance.id).update(title=title)
    except:
        pass


class Vote(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    claim = models.ForeignKey(Claim)
    # TODO: Vote.approved needs null=True or blank=False
    approved = models.BooleanField(default=None, blank=True)
    created = models.DateTimeField(null=True, blank=True)
    modified = models.DateTimeField(null=True, blank=True, auto_now=True)

    class Meta:
        unique_together = (("user", "claim"),)

    def __unicode__(self):
        return u'Vote for %s by (%s): %s' % (
            self.claim, self.user, self.approved
        )


@receiver(post_save, sender=Vote)
def update_claim_status(sender, instance, created, **kwargs):
    claim = instance.claim
    offers_needed = claim.offers.count()

    if claim.num_votes > 0:
        claim.status = "Pending"
        claim.save()

    if claim.num_approvals == offers_needed:
        claim.status = "Approved"
        claim.save()

    if offers_needed > 0:
        if (claim.num_rejections / float(offers_needed)) >= 0.5:
            claim.status = "Rejected"
            claim.save()


@receiver(post_save, sender=Vote)
def notify_approved_claim(sender, instance, created, **kwargs):
    claim = instance.claim
    votes_needed = claim.offers.count()

    if claim.num_rejections == votes_needed:
        current_site = Site.objects.get_current()
        # TODO: make a nicer HTML email template
        CLAIM_REJECTED_EMAIL_STRING = """
        Your claim for {url} has been rejected.
        https://{site}
        """
        send_mail(
            "[codesy] Your claimed has been rejected",
            CLAIM_REJECTED_EMAIL_STRING.format(
                url=claim.issue.url,
                site=current_site,
            ),
            settings.DEFAULT_FROM_EMAIL,
            [claim.user.email]
        )

    if votes_needed == claim.num_approvals:
        current_site = Site.objects.get_current()
        # TODO: make a nicer HTML email template
        CLAIM_APPROVED_EMAIL_STRING = """
        Your claim for {url} has been approved.
        https://{site}
        """
        send_mail(
            "[codesy] Your claimed has been approved",
            CLAIM_APPROVED_EMAIL_STRING.format(
                url=claim.issue.url,
                site=current_site,
            ),
            settings.DEFAULT_FROM_EMAIL,
            [claim.user.email]
        )


class Payment(models.Model):
    PROVIDER_CHOICES = (
        ('Stripe', 'Stripe'),
        ('PayPal', 'PayPal'),
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    amount = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, default=0)
    transaction_key = models.CharField(
        max_length=255, default=uuid.uuid4, blank=True)
    api_success = models.BooleanField(default=False)
    error_message = models.CharField(max_length=255, blank=True)
    charge_amount = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, default=0)
    confirmation = models.CharField(max_length=255, blank=True)
    created = models.DateTimeField(null=True, blank=True)
    modified = models.DateTimeField(null=True, blank=True, auto_now=True)

    def short_key(self):
        return (self.transaction_key
                .bytes.encode('base64').rstrip('=\n').replace('/', '_'))

    class Meta:
        abstract = True


class Offer(Payment):
    bid = models.ForeignKey(Bid, related_name='payments')
    provider = models.CharField(
        max_length=255,
        choices=Payment.PROVIDER_CHOICES,
        default='Stripe')

    def fees(self):
        return OfferFee.objects.filter(offer=self)

    def __unicode__(self):
        return u'Offer payment for bid (%s) paid' % (
            self.bid.id
        )

    def request(self):
        codesy_pct = Decimal('0.025')

        codesy_fee = OfferFee(
            offer=self,
            fee_type='codesy',
            amount=self.amount * codesy_pct
        )
        codesy_fee.save()

        stripe_pct = Decimal('0.029')
        stripe_transaction = Decimal('0.30')

        stripe_charge = (
            (self.amount + codesy_fee.amount + stripe_transaction)
            / (1 - stripe_pct)
        )

        stripe_fee = stripe_charge - (self.amount + codesy_fee.amount)

        stripe_fee = OfferFee(
            offer=self,
            fee_type='Stripe',
            amount=stripe_fee
        )
        stripe_fee.save()

        # get rounded fee values
        fees = OfferFee.objects.filter(offer=self)
        sum_fees = fees.aggregate(Sum('amount'))['amount__sum']

        stripe_charge = self.amount + sum_fees

        # TODO: removed with proper stripe mocking in tests
        self.charge_amount = stripe_charge
        self.save()

        # TODO: HANDLE CARD NOT YET REGISTERED
        try:
            charge = stripe.Charge.create(
                amount=int(stripe_charge * 100),
                currency="usd",
                customer=self.user.stripe_account_token,
                description="Offer for: " + self.bid.url,
                metadata={'id': self.id}
            )
            if charge:
                self.charge_amount = stripe_charge
                self.confirmation = charge.id
                self.api_success = True
                self.offer = self
                self.save()
            else:
                self.error_message = "Charge failed, try later"
                self.save()
                return False
        except Exception as e:
            self.error_message = e.message
            self.save()
            return False

        return True


class Payout(Payment):
    claim = models.ForeignKey(Claim, related_name='payouts')
    provider = models.CharField(
        max_length=255,
        choices=Payment.PROVIDER_CHOICES,
        default='PayPal')

    def __unicode__(self):
        return u'Payout to %s for claim (%s)' % (
            self.user, self.claim.id
        )

    def fees(self):
        return PayoutFee.objects.filter(payout=self)

    def credits(self):
        return PayoutCredit.objects.filter(payout=self)

    def request(self):
        receiver = (
            PAYPAL_PAYOUT_RECIPIENT if PAYPAL_PAYOUT_RECIPIENT
            else self.claim.user.email
        )

        total_payout_amount = self.amount

        paypal_fee = PayoutFee(
            payout=self,
            fee_type='PayPal',
            amount=Decimal('0.25')
        )
        paypal_fee.save()

        try:
            user_offers = Offer.objects.filter(user=self.claim.user)
            if user_offers:
                total_refund = (
                    user_offers.aggregate(Sum('amount'))['amount__sum']
                )
                refund_user_offer = PayoutCredit(
                    payout=self,
                    fee_type='refund',
                    description="Your offer",
                    amount=total_refund,
                )
                refund_user_offer.save()
                total_payout_amount += total_refund

        except Offer.DoesNotExist:
            refund_user_offer = None

        codesy_fee = PayoutFee(
            payout=self,
            fee_type='codesy',
            amount=total_payout_amount * Decimal('0.025'),
        )
        codesy_fee.save()

        total_fees = paypal_fee.amount + codesy_fee.amount
        self.charge_amount = total_payout_amount - total_fees
        self.save()
        # attempt paypal payout
        # user generated id sent to paypal is limited to 30 chars
        sender_id = self.short_key()
        paypal_payout = PaypalPayout({
            "sender_batch_header": {
                "sender_batch_id": sender_id,
                "email_subject": "Your codesy payout is here!"
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "amount": {
                        "value": int(self.charge_amount),
                        "currency": "USD"
                    },
                    "receiver": receiver,
                    "note": "Here's your payout for fixing an issue.",
                    "sender_item_id": sender_id
                }
            ]
        })
        try:
            payout_attempt = paypal_payout.create(sync_mode=True)
        except:
            payout_attempt = False

        if payout_attempt:
            if paypal_payout.items:
                for item in paypal_payout.items:
                    if item.transaction_status == "SUCCESS":
                        self.api_success = True
                        self.confirmation = item.payout_item_id
                        self.save()
                    else:
                        payout_attempt = False
        return payout_attempt


class Fee(models.Model):
    FEE_TYPES = (
        ('PayPal', 'PayPal'),
        ('Stripe', 'Stripe'),
        ('codesy', 'codesy'),
        ('refund', 'refund')
    )
    fee_type = models.CharField(
        max_length=255,
        choices=FEE_TYPES,
        default='')
    amount = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, default=0)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        abstract = True


class OfferFee(Fee):
    offer = models.ForeignKey(Offer, related_name="offer_fees", null=True)


class PayoutFee(Fee):
    payout = models.ForeignKey(Payout, related_name="payout_fees", null=True)


class PayoutCredit(Fee):
    payout = models.ForeignKey(Payout, related_name="payout_credit", null=True)


@receiver(pre_save, sender=OfferFee)
@receiver(pre_save, sender=PayoutFee)
def roundup_penny(sender, instance, *args, **kwargs):
    instance.amount = (instance.amount.quantize(Decimal('.01'),
                       rounding=ROUND_UP))


@receiver(post_save, sender=Payout)
@receiver(post_save, sender=Offer)
@receiver(post_save, sender=Bid)
@receiver(post_save, sender=Claim)
@receiver(post_save, sender=Vote)
def update_datetimes_for_model_save(sender, instance, created, **kwargs):
    if created:
        sender.objects.filter(id=instance.id).update(created=datetime.now())
    else:
        sender.objects.filter(id=instance.id).update(modified=datetime.now())
