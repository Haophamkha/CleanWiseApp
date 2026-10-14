from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.ai_engine.models import ChatbotSession
from apps.common.throttling import WriteScopedThrottleMixin
from .exceptions import ChatbotError
from .models import ChatbotRun
from .permissions import IsChatbotUser
from .serializers import (ConversationCreateSerializer, ConversationSerializer,
                          ConversationUpdateSerializer, MessageInputSerializer,
                          MessageReplySerializer, MessageSerializer, RunDetailSerializer, RunSerializer)
from .services.conversation import delete_conversation, get_conversation, send_message
from .services.knowledge import published_articles
from .serializers import HelpArticleSerializer


class HelpArticleListView(generics.ListAPIView):
    permission_classes = [IsChatbotUser]
    serializer_class = HelpArticleSerializer
    pagination_class = None

    def get_queryset(self):
        return published_articles(self.request.user.role).defer('keywords')


class HelpArticleDetailView(generics.RetrieveAPIView):
    permission_classes = [IsChatbotUser]
    serializer_class = HelpArticleSerializer

    def get_queryset(self):
        return published_articles(self.request.user.role)


class ChatbotPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 50


@extend_schema_view(
    get=extend_schema(tags=['Chatbot - Customer'], summary='Danh sách hội thoại của khách'),
    post=extend_schema(tags=['Chatbot - Customer'], summary='Tạo hội thoại',
                       request=ConversationCreateSerializer, responses={201: ConversationSerializer}),
)
class ConversationListCreateView(WriteScopedThrottleMixin, generics.ListCreateAPIView):
    permission_classes = [IsChatbotUser]
    serializer_class = ConversationSerializer
    pagination_class = ChatbotPagination
    write_throttle_scope = 'chatbot'

    def get_queryset(self):
        return ChatbotSession.objects.filter(customer=self.request.user).order_by('-updated_at', '-id')

    def create(self, request, *args, **kwargs):
        serializer = ConversationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = ChatbotSession.objects.create(customer=request.user, **serializer.validated_data)
        return Response(ConversationSerializer(conversation).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(tags=['Chatbot - Customer'], summary='Chi tiết hội thoại'),
    patch=extend_schema(tags=['Chatbot - Customer'], summary='Đổi tiêu đề hoặc đóng/mở hội thoại',
                        request=ConversationUpdateSerializer, responses=ConversationSerializer),
    delete=extend_schema(tags=['Chatbot - Customer'], summary='Xóa hội thoại và lịch sử tin nhắn',
                         responses={204: None}),
)
class ConversationDetailView(WriteScopedThrottleMixin, generics.GenericAPIView):
    permission_classes = [IsChatbotUser]
    serializer_class = ConversationSerializer
    write_throttle_scope = 'chatbot'

    def get_object(self):
        return get_conversation(self.request.user, self.kwargs['pk'])

    def get(self, request, *args, **kwargs):
        return Response(ConversationSerializer(self.get_object()).data)

    def delete(self, request, *args, **kwargs):
        delete_conversation(request.user, self.kwargs['pk'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    def patch(self, request, *args, **kwargs):
        from django.db import transaction

        serializer = ConversationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            conversation = get_object_or_404(ChatbotSession.objects.select_for_update(),
                                              pk=self.kwargs['pk'], customer=request.user)
            if conversation.runs.filter(status=ChatbotRun.Status.RUNNING).exists():
                raise ChatbotError('Hãy chờ tin nhắn đang xử lý hoàn tất.', code='CONVERSATION_BUSY', status_code=409)
            for field, value in serializer.validated_data.items():
                setattr(conversation, field, value)
            conversation.save(update_fields=[*serializer.validated_data, 'updated_at'])
        return Response(ConversationSerializer(conversation).data)


@extend_schema_view(
    get=extend_schema(tags=['Chatbot - Customer'], summary='Lịch sử tin nhắn, cũ đến mới'),
    post=extend_schema(tags=['Chatbot - Customer'], summary='Gửi tin nhắn tới Gemini agent',
                       request=MessageInputSerializer,
                       responses={200: MessageReplySerializer, 201: MessageReplySerializer}),
)
class MessageListCreateView(WriteScopedThrottleMixin, generics.ListAPIView):
    permission_classes = [IsChatbotUser]
    serializer_class = MessageSerializer
    pagination_class = ChatbotPagination
    write_throttle_scope = 'chatbot'

    def get_queryset(self):
        conversation = get_conversation(self.request.user, self.kwargs['pk'])
        return conversation.messages.select_related('chatbot_run', 'reply_run').order_by('id')

    def post(self, request, *args, **kwargs):
        serializer = MessageInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run, replayed = send_message(request.user, self.kwargs['pk'], **serializer.validated_data)
        data = {'conversation_id': run.conversation_id, 'run': RunSerializer(run).data,
                'user_message': MessageSerializer(run.user_message).data,
                'assistant_message': MessageSerializer(run.assistant_message).data, 'replayed': replayed}
        return Response(data, status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED)


@extend_schema_view(get=extend_schema(tags=['Chatbot - Customer'], summary='Trạng thái lượt trả lời sau lỗi mạng',
                                    responses=RunDetailSerializer))
class RunDetailView(generics.GenericAPIView):
    permission_classes = [IsChatbotUser]
    serializer_class = RunSerializer

    def get(self, request, pk):
        run = get_object_or_404(ChatbotRun, pk=pk, conversation__customer=request.user)
        return Response({'run': RunSerializer(run).data,
                         'user_message': MessageSerializer(run.user_message).data,
                         'assistant_message': MessageSerializer(run.assistant_message).data if run.assistant_message_id else None})