from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from auctions.models import Bid, Claim, Vote

from .serializers import BidSerializer, ClaimSerializer, VoteSerializer


class AutoOwnObjectsModelViewSet(ModelViewSet):
    """
    Custom ModelViewSet that automatically:
        1. assigns obj.user to self.request.user
        2. restricts queryset to users' own objects
    """
    def pre_save(self, obj):
        obj.user = self.request.user

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)


class BidViewSet(AutoOwnObjectsModelViewSet):
    """
    API endpoint for bids. Users can only access their own bids.
    """
    queryset = Bid.objects.all()
    serializer_class = BidSerializer


class BidAPIView(APIView):
    """
    API endpoint for a single bid form.

    Requests for /bid/?url= will receive the HTML form for creating a bid (if
    none exists) or updating the user's existing bid.

    url -- url of an OSS issue or bug
    """
    renderer_classes = (TemplateHTMLRenderer,)

    def get(self, request, format=None):
        url = self.request.query_params.get('url')
        bid = None
        claim = None
        try:
            bid = Bid.objects.get(user=self.request.user, url=url)
        except Bid.DoesNotExist:
            pass
        else:
            try:
                claim = Claim.objects.get(
                    user=self.request.user, issue=bid.issue
                )
            except Claim.DoesNotExist:
                pass

        resp = Response({'bid': bid,
                         'claim': claim,
                         'url': url},
                        template_name='bid.html')
        return resp


class ClaimViewSet(AutoOwnObjectsModelViewSet):
    """
    API endpoint for claims. Users can only access their own claims.
    """
    queryset = Claim.objects.all()
    serializer_class = ClaimSerializer


class ClaimAPIView(APIView):
    """
    Custom API endpoint for user-centric Claim status page

    Requests for /claim-status/{id} will receive the claim details, along with
    an HTML form for offering bidders to approve or deny the claim.

    id -- id of claim
    """
    renderer_classes = (TemplateHTMLRenderer,)

    def get(self, request, pk, format=None):
        try:
            claim = Claim.objects.get(pk=pk)
        except Claim.DoesNotExist:
            claim = None

        return Response({'claim': claim}, template_name='claim_status.html')


class VoteViewSet(AutoOwnObjectsModelViewSet):
    """
    API endpoint for votes. Users can only access their own votes.
    """
    queryset = Vote.objects.all()
    serializer_class = VoteSerializer
