import { useCallback, useDeferredValue, useEffect, useRef, useState } from "react";
import { ArrowLeft, RefreshCcw, Search, Users } from "lucide-react";
import { api, type JuheConversationDetailResponse, type JuheConversationSummary, type JuheMessageItem } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Markdown } from "@/components/Markdown";
import { cn, timeAgo } from "@/lib/utils";

const POLL_INTERVAL_MS = 5000;
const CONVERSATION_LIMIT = 200;
const MESSAGE_PAGE_SIZE = 50;
const MESSAGE_BOTTOM_THRESHOLD_PX = 48;

function formatClock(ts: number | null | undefined): string {
  if (!ts) return "--:--";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(ts * 1000));
}

function formatDay(ts: number): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    weekday: "short",
  }).format(new Date(ts * 1000));
}

function messageDayKey(ts: number): string {
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

function getMemberLabel(member: Record<string, unknown>): string {
  const nickname = String(member.nickname ?? "").trim();
  if (nickname) return nickname;
  const displayName = String(member.display_name ?? "").trim();
  if (displayName) return displayName;
  const roomRemark = String(member.roomname_remark ?? "").trim();
  if (roomRemark) return roomRemark;
  const name = String(member.name ?? "").trim();
  if (name) return name;
  const uin = String(member.uin ?? "").trim();
  if (uin) return `UIN ${uin}`;
  return "Unknown member";
}

function hasMemberDisplayName(member: Record<string, unknown>): boolean {
  return [
    member.nickname,
    member.display_name,
    member.roomname_remark,
    member.name,
  ].some((value) => String(value ?? "").trim().length > 0);
}

function getBottomScrollTop(element: HTMLElement): number {
  return Math.max(0, element.scrollHeight - element.clientHeight);
}

function isNearBottom(element: HTMLElement): boolean {
  return getBottomScrollTop(element) - element.scrollTop <= MESSAGE_BOTTOM_THRESHOLD_PX;
}

function setElementScrollTop(element: HTMLElement, top: number): void {
  const nextTop = Math.max(0, top);
  if (typeof element.scrollTo === "function") {
    element.scrollTo({ top: nextTop, behavior: "auto" });
    return;
  }
  element.scrollTop = nextTop;
}

function MessageTimeline({ messages }: { messages: JuheMessageItem[] }) {
  let lastDay = "";

  return (
    <div className="flex flex-col gap-3 p-4 sm:p-6">
      {messages.length === 0 ? (
        <div className="border border-border bg-card/70 px-4 py-6 text-sm text-muted-foreground">
          No messages yet.
        </div>
      ) : messages.map((message) => {
        const dayKey = messageDayKey(message.timestamp);
        const showDayDivider = dayKey !== lastDay;
        lastDay = dayKey;
        const isOutbound = message.direction === "outbound";

        return (
          <div key={message.id}>
            {showDayDivider ? (
              <div className="mb-3 flex items-center justify-center">
                <span className="border border-border bg-background/80 px-3 py-1 font-display text-[0.65rem] uppercase tracking-[0.16em] text-muted-foreground">
                  {formatDay(message.timestamp)}
                </span>
              </div>
            ) : null}

            <div className={cn("flex", isOutbound ? "justify-end" : "justify-start")}>
              <div
                className={cn(
                  "max-w-[88%] border px-4 py-3 sm:max-w-[78%]",
                  isOutbound
                    ? "border-foreground/25 bg-foreground/10 text-foreground"
                    : "border-border bg-card/80 text-foreground",
                )}
              >
                <div className="mb-2 flex items-center gap-2 text-[0.65rem] uppercase tracking-[0.14em] text-muted-foreground">
                  <span>{message.sender_name || message.sender_id || "Unknown"}</span>
                  <span className="text-border">&#183;</span>
                  <span>{formatClock(message.timestamp)}</span>
                </div>
                {!message.is_text ? (
                  <div className="mb-2">
                    <Badge variant="outline" className="w-fit">
                      {message.message_type_label}
                    </Badge>
                  </div>
                ) : null}
                <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">
                  {message.is_text ? (message.text || message.preview) : message.preview}
                </p>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function JuheWorkspacePage() {
  const [conversations, setConversations] = useState<JuheConversationSummary[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [detail, setDetail] = useState<JuheConversationDetailResponse | null>(null);
  const [messages, setMessages] = useState<JuheMessageItem[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [mobileView, setMobileView] = useState<"list" | "detail">("list");

  const deferredSearch = useDeferredValue(searchQuery);
  const detailRequestId = useRef(0);
  const messageScrollRef = useRef<HTMLDivElement | null>(null);
  const previousConversationIdRef = useRef<string | null>(null);
  const lastMessageScrollTopRef = useRef(0);
  const keepMessageViewPinnedRef = useRef(true);

  const visibleConversations = conversations.filter((conversation) => {
    const query = deferredSearch.trim().toLowerCase();
    if (!query) return true;
    return [
      conversation.title,
      conversation.room_id,
      conversation.last_message_preview,
      conversation.last_message_sender_name,
    ]
      .join(" ")
      .toLowerCase()
      .includes(query);
  });

  const loadConversationList = useCallback(async () => {
    setListError(null);
    setListLoading(true);
    try {
      const response = await api.getJuheConversations({ limit: CONVERSATION_LIMIT });
      setConversations(response.conversations);
      setSelectedConversationId((current) => {
        if (current && response.conversations.some((item) => item.conversation_id === current)) {
          return current;
        }
        return response.conversations[0]?.conversation_id ?? null;
      });
    } catch (error) {
      setListError(error instanceof Error ? error.message : String(error));
    } finally {
      setListLoading(false);
    }
  }, []);

  const loadConversationView = useCallback(async (conversationId: string) => {
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    setDetailLoading(true);
    setDetailError(null);

    try {
      const [detailResponse, messageResponse] = await Promise.all([
        api.getJuheConversationDetail(conversationId),
        api.getJuheConversationMessages(conversationId, { limit: MESSAGE_PAGE_SIZE }),
      ]);

      if (detailRequestId.current !== requestId) return;
      setDetail(detailResponse);
      setMessages(messageResponse.messages);
    } catch (error) {
      if (detailRequestId.current !== requestId) return;
      setDetailError(error instanceof Error ? error.message : String(error));
      setDetail(null);
      setMessages([]);
    } finally {
      if (detailRequestId.current === requestId) {
        setDetailLoading(false);
      }
    }
  }, []);

  const refreshVisibleData = useCallback(async () => {
    await loadConversationList();
    if (selectedConversationId) {
      await loadConversationView(selectedConversationId);
    }
  }, [loadConversationList, loadConversationView, selectedConversationId]);

  const handleMessageScroll = useCallback(() => {
    const element = messageScrollRef.current;
    if (!element) return;
    lastMessageScrollTopRef.current = element.scrollTop;
    keepMessageViewPinnedRef.current = isNearBottom(element);
  }, []);

  useEffect(() => {
    void loadConversationList();
  }, [loadConversationList]);

  useEffect(() => {
    if (!selectedConversationId) {
      setDetail(null);
      setMessages([]);
      setDetailLoading(false);
      return;
    }
    void loadConversationView(selectedConversationId);
  }, [loadConversationView, selectedConversationId]);

  useEffect(() => {
    if (visibleConversations.length === 0) {
      return;
    }
    if (selectedConversationId && visibleConversations.some((item) => item.conversation_id === selectedConversationId)) {
      return;
    }
    setSelectedConversationId(visibleConversations[0].conversation_id);
  }, [selectedConversationId, visibleConversations]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void refreshVisibleData();
    }, POLL_INTERVAL_MS);

    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void refreshVisibleData();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [refreshVisibleData]);

  useEffect(() => {
    const element = messageScrollRef.current;
    if (!element) return;

    if (!selectedConversationId) {
      previousConversationIdRef.current = null;
      lastMessageScrollTopRef.current = 0;
      keepMessageViewPinnedRef.current = true;
      setElementScrollTop(element, 0);
      return;
    }

    if (detailLoading) {
      return;
    }

    const conversationChanged = previousConversationIdRef.current !== selectedConversationId;
    const shouldStickToBottom = conversationChanged || keepMessageViewPinnedRef.current;
    const nextTop = shouldStickToBottom
      ? getBottomScrollTop(element)
      : Math.min(lastMessageScrollTopRef.current, getBottomScrollTop(element));

    setElementScrollTop(element, nextTop);
    lastMessageScrollTopRef.current = nextTop;
    keepMessageViewPinnedRef.current = shouldStickToBottom ? true : isNearBottom(element);
    previousConversationIdRef.current = selectedConversationId;
  }, [detailLoading, messages, selectedConversationId]);

  const selectedConversation = detail?.conversation
    || conversations.find((item) => item.conversation_id === selectedConversationId)
    || null;
  const roomMemory = detail?.room_memory;
  const showRoomMemory = Boolean(roomMemory?.exists && roomMemory.content);
  const detailMembers = detail?.members ?? [];
  const membersLackDisplayNames = detailMembers.length > 0 && detailMembers.every((member) => !hasMemberDisplayName(member));

  return (
    <div data-testid="juhe-root" className="flex h-screen flex-col bg-background text-foreground overflow-hidden">
      <div className="noise-overlay" />
      <div className="warm-glow" />

      <div data-testid="juhe-workspace" className="relative z-10 flex min-h-0 flex-1 flex-col lg:flex-row">
        <aside className={cn(
          "min-h-0 border-b border-border bg-card/75 lg:flex lg:w-[340px] lg:flex-col lg:border-b-0 lg:border-r",
          mobileView === "detail" ? "hidden lg:flex" : "flex",
        )}>
          <div className="border-b border-border px-4 py-4 sm:px-5">
            <div className="mb-4 flex items-start justify-between gap-3">
              <div>
                <p className="font-display text-[0.72rem] uppercase tracking-[0.2em] text-muted-foreground">
                  Hermes x Juhe
                </p>
                <h1 className="mt-2 font-collapse text-2xl tracking-[0.06em] uppercase">
                  Juhe Workspace
                </h1>
                <p className="mt-2 max-w-xs text-sm text-muted-foreground">
                  Read-only group conversation view with live polling.
                </p>
              </div>
              <Button
                variant="outline"
                size="icon"
                aria-label="Refresh conversations"
                onClick={() => { void refreshVisibleData(); }}
              >
                <RefreshCcw className="h-4 w-4" />
              </Button>
            </div>

            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                placeholder="Search groups..."
                className="pl-9"
              />
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {listLoading ? (
              <div className="px-4 py-6 text-sm text-muted-foreground">Loading groups...</div>
            ) : listError ? (
              <div className="px-4 py-6 text-sm text-destructive">
                Failed to load conversations: {listError}
              </div>
            ) : visibleConversations.length === 0 ? (
              <div className="px-4 py-6 text-sm text-muted-foreground">No groups found.</div>
            ) : (
              visibleConversations.map((conversation) => (
                <button
                  key={conversation.conversation_id}
                  type="button"
                  className={cn(
                    "w-full border-b border-border px-4 py-4 text-left transition-colors hover:bg-foreground/5",
                    conversation.conversation_id === selectedConversationId ? "bg-foreground/8" : "",
                  )}
                  onClick={() => {
                    setSelectedConversationId(conversation.conversation_id);
                    setMobileView("detail");
                  }}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate font-display text-sm uppercase tracking-[0.08em]">
                        {conversation.title}
                      </div>
                      <p className="mt-2 line-clamp-2 text-sm text-muted-foreground">
                        {conversation.last_message_preview || "No messages yet"}
                      </p>
                    </div>
                    <div className="shrink-0 text-[0.65rem] uppercase tracking-[0.15em] text-muted-foreground">
                      {conversation.last_message_at ? timeAgo(conversation.last_message_at) : "idle"}
                    </div>
                  </div>
                  <div className="mt-3 flex items-center gap-2 text-[0.65rem] uppercase tracking-[0.14em] text-muted-foreground">
                    {conversation.member_count ? (
                      <>
                        <Users className="h-3.5 w-3.5" />
                        <span>{conversation.member_count}</span>
                      </>
                    ) : null}
                    {conversation.last_message_type_label && !conversation.last_message_preview ? (
                      <Badge variant="outline">{conversation.last_message_type_label}</Badge>
                    ) : null}
                  </div>
                </button>
              ))
            )}
          </div>
        </aside>

        <section className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col",
          mobileView === "list" ? "hidden lg:flex" : "flex",
        )}>
          <header className="border-b border-border bg-background/85 px-4 py-4 backdrop-blur-sm sm:px-6">
            <div className="flex items-center gap-3">
              <Button
                variant="ghost"
                size="icon"
                className="lg:hidden"
                aria-label="Back to list"
                onClick={() => setMobileView("list")}
              >
                <ArrowLeft className="h-4 w-4" />
              </Button>
              <div className="min-w-0 flex-1">
                <h2 className="truncate font-collapse text-xl uppercase tracking-[0.06em]">
                  {selectedConversation?.title || "Choose a group"}
                </h2>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[0.7rem] uppercase tracking-[0.14em] text-muted-foreground">
                  {selectedConversation ? (
                    <>
                      <span>{selectedConversation.conversation_id}</span>
                      {selectedConversation.member_count ? (
                        <>
                          <span className="text-border">&#183;</span>
                          <span>{selectedConversation.member_count} members</span>
                        </>
                      ) : null}
                    </>
                  ) : (
                    <span>Select a conversation from the list.</span>
                  )}
                </div>
              </div>
              <Button variant="outline" onClick={() => { void refreshVisibleData(); }}>
                <RefreshCcw className="h-4 w-4" />
                Refresh
              </Button>
            </div>
          </header>

          <div
            ref={messageScrollRef}
            data-testid="juhe-message-scroll"
            className="min-h-0 flex-1 overflow-y-auto"
            onScroll={handleMessageScroll}
          >
            {detailLoading ? (
              <div className="px-4 py-6 text-sm text-muted-foreground sm:px-6">Loading messages...</div>
            ) : detailError ? (
              <div className="px-4 py-6 text-sm text-destructive sm:px-6">
                Failed to load conversation: {detailError}
              </div>
            ) : !selectedConversationId ? (
              <div className="px-4 py-6 text-sm text-muted-foreground sm:px-6">
                No conversation selected.
              </div>
            ) : (
              <MessageTimeline messages={messages} />
            )}
          </div>
        </section>

        <aside
          data-testid="juhe-info-panel"
          className="hidden min-h-0 w-[320px] border-l border-border bg-card/70 xl:flex xl:flex-col"
        >
          <div className="border-b border-border px-5 py-5">
            <p className="font-display text-[0.72rem] uppercase tracking-[0.2em] text-muted-foreground">
              Group Info
            </p>
            <h3 className="mt-3 font-collapse text-xl uppercase tracking-[0.06em]">
              {selectedConversation?.title || "No group selected"}
            </h3>
          </div>

          <div
            data-testid="juhe-info-scroll"
            className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5"
          >
            {selectedConversation ? (
              <>
                <div>
                  <div className="text-[0.68rem] uppercase tracking-[0.16em] text-muted-foreground">Conversation ID</div>
                  <div className="mt-2 font-courier text-sm">{selectedConversation.conversation_id}</div>
                </div>
                {selectedConversation.member_count ? (
                  <div>
                    <div className="text-[0.68rem] uppercase tracking-[0.16em] text-muted-foreground">Members</div>
                    <div className="mt-2 text-sm">{selectedConversation.member_count}</div>
                  </div>
                ) : null}
                <div>
                  <div className="text-[0.68rem] uppercase tracking-[0.16em] text-muted-foreground">Recent Activity</div>
                  <div className="mt-2 text-sm">
                    {selectedConversation.last_message_at ? timeAgo(selectedConversation.last_message_at) : "No history yet"}
                  </div>
                </div>
                {detail?.members.length ? (
                  <div>
                    <div className="mb-3 text-[0.68rem] uppercase tracking-[0.16em] text-muted-foreground">
                      Member Directory
                    </div>
                    {membersLackDisplayNames ? (
                      <div className="mb-3 text-sm text-muted-foreground">
                        Display names are not cached for this group yet. Showing member UINs.
                      </div>
                    ) : null}
                    <div className="space-y-2">
                      {detail.members.map((member, index) => (
                        <div
                          key={`${String(member.uin ?? member.nickname ?? index)}`}
                          className="border border-border bg-background/60 px-3 py-2 text-sm"
                        >
                          {getMemberLabel(member)}
                        </div>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="text-sm text-muted-foreground">No cached member list for this group yet.</div>
                )}
                {showRoomMemory ? (
                  <div>
                    <div className="mb-3 text-[0.68rem] uppercase tracking-[0.16em] text-muted-foreground">
                      Room Memory
                    </div>
                    {roomMemory?.path ? (
                      <div className="mb-3 break-all font-courier text-[0.72rem] text-muted-foreground">
                        {roomMemory.path}
                      </div>
                    ) : null}
                    <div className="border border-border bg-background/60 px-3 py-3">
                      <Markdown content={roomMemory?.content || ""} />
                    </div>
                  </div>
                ) : null}
              </>
            ) : (
              <div className="text-sm text-muted-foreground">Pick a group to inspect its metadata.</div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
