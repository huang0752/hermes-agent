import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { I18nProvider } from "@/i18n";

function renderAppAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <I18nProvider>
        <App />
      </I18nProvider>
    </MemoryRouter>,
  );
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function installScrollMock() {
  let scrollTopValue = 0;
  const scrollToMock = vi.fn((options?: ScrollToOptions | number, y?: number) => {
    if (typeof options === "number") {
      scrollTopValue = y ?? options;
      return;
    }
    scrollTopValue = Number(options?.top ?? 0);
  });

  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get() {
      return 1000;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      return 200;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTop", {
    configurable: true,
    get() {
      return scrollTopValue;
    },
    set(value: number) {
      scrollTopValue = value;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    value: scrollToMock,
  });

  return {
    getScrollTop: () => scrollTopValue,
    scrollToMock,
    setScrollTop: (value: number) => {
      scrollTopValue = value;
    },
  };
}

describe("Juhe workspace route", () => {
  const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    window.__HERMES_SESSION_TOKEN__ = "test-token";
  });

  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    window.localStorage?.clear?.();
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("renders the standalone juhe workspace with read-only messages", async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/juhe/conversations?")) {
        return jsonResponse({
          conversations: [
            {
              conversation_id: "R:2001",
              room_id: "2001",
              title: "Dev Group",
              member_count: 2,
              last_message_preview: "Latest update",
              last_message_sender_name: "Alice",
              last_message_at: 1710000200,
              has_messages: true,
            },
          ],
          total: 1,
          query: "",
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001/messages")) {
        return jsonResponse({
          conversation_id: "R:2001",
          has_more: false,
          messages: [
            {
              id: "msg-1",
              message_id: "msg-1",
              preview: "Hello from Juhe",
              text: "Hello from Juhe",
              is_text: true,
              message_type: 2,
              message_type_label: "文本",
              timestamp: 1710000100,
              direction: "inbound",
              sender_name: "Alice",
            },
          ],
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001")) {
        return jsonResponse({
          conversation: {
            conversation_id: "R:2001",
            room_id: "2001",
            title: "Dev Group",
            member_count: 2,
            last_message_preview: "Latest update",
            last_message_sender_name: "Alice",
            last_message_at: 1710000200,
            has_messages: true,
          },
          members: [
            { uin: "1001", nickname: "Alice" },
            { uin: "1002", nickname: "Bob" },
          ],
          room_memory: {
            exists: true,
            path: "/Users/chou/.hermes/memories/juhe/rooms/2001/MEMORY.md",
            content: "# MEMORY\n\n- 群内需求以最终确认版本为准。",
          },
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    renderAppAt("/juhe");

    expect(await screen.findByText("Juhe Workspace")).toBeInTheDocument();
    expect((await screen.findAllByText("Dev Group")).length).toBeGreaterThan(0);
    expect(await screen.findByText("Hello from Juhe")).toBeInTheDocument();
    expect(await screen.findByText("Room Memory")).toBeInTheDocument();
    expect(await screen.findByText("群内需求以最终确认版本为准。")).toBeInTheDocument();
    expect(screen.queryByText("Status")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /send/i })).not.toBeInTheDocument();
  });

  it("filters the conversation list from the left search input", async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/juhe/conversations?")) {
        return jsonResponse({
          conversations: [
            {
              conversation_id: "R:2001",
              room_id: "2001",
              title: "Dev Group",
              member_count: 2,
              last_message_preview: "Latest update",
              last_message_sender_name: "Alice",
              last_message_at: 1710000200,
              has_messages: true,
            },
            {
              conversation_id: "R:2002",
              room_id: "2002",
              title: "Ops Group",
              member_count: 3,
              last_message_preview: "Ops note",
              last_message_sender_name: "Bob",
              last_message_at: 1710000100,
              has_messages: true,
            },
          ],
          total: 2,
          query: "",
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001/messages")) {
        return jsonResponse({ conversation_id: "R:2001", has_more: false, messages: [] });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001")) {
        return jsonResponse({
          conversation: {
            conversation_id: "R:2001",
            room_id: "2001",
            title: "Dev Group",
            member_count: 2,
            has_messages: true,
          },
          members: [],
          room_memory: {
            exists: false,
            path: null,
            content: "",
          },
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    renderAppAt("/juhe");

    expect((await screen.findAllByText("Dev Group")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("Ops Group")).length).toBeGreaterThan(0);

    fireEvent.change(screen.getByPlaceholderText("Search groups..."), {
      target: { value: "ops" },
    });

    await waitFor(() => {
      expect(screen.queryAllByText("Dev Group")).toHaveLength(0);
    });
    expect(screen.getAllByText("Ops Group").length).toBeGreaterThan(0);
    expect(screen.queryByText("Room Memory")).not.toBeInTheDocument();
  });

  it("polls while visible, pauses when hidden, and refreshes on visibility restore", async () => {
    let intervalCallback: (() => void) | null = null;
    const setIntervalMock = (handler: TimerHandler, _timeout?: number, ...args: unknown[]): number => {
      intervalCallback = () => {
        if (typeof handler === "function") {
          (handler as (...callbackArgs: unknown[]) => void)(...args);
        }
      };
      return 1;
    };
    vi.spyOn(window, "setInterval").mockImplementation(setIntervalMock as unknown as typeof window.setInterval);

    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/juhe/conversations?")) {
        return jsonResponse({
          conversations: [
            {
              conversation_id: "R:2001",
              room_id: "2001",
              title: "Dev Group",
              member_count: 2,
              last_message_preview: "Latest update",
              last_message_sender_name: "Alice",
              last_message_at: 1710000200,
              has_messages: true,
            },
          ],
          total: 1,
          query: "",
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001/messages")) {
        return jsonResponse({ conversation_id: "R:2001", has_more: false, messages: [] });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001")) {
        return jsonResponse({
          conversation: {
            conversation_id: "R:2001",
            room_id: "2001",
            title: "Dev Group",
            member_count: 2,
            has_messages: true,
          },
          members: [],
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    renderAppAt("/juhe");

    expect(await screen.findByRole("heading", { name: "Juhe Workspace", level: 1 })).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3);
    });
    const initialConversationCalls = fetchMock.mock.calls.length;
    expect(intervalCallback).not.toBeNull();

    await act(async () => {
      intervalCallback?.();
    });

    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(initialConversationCalls);
    });

    const afterVisiblePoll = fetchMock.mock.calls.length;
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    fireEvent(document, new Event("visibilitychange"));

    await act(async () => {
      intervalCallback?.();
    });

    expect(fetchMock.mock.calls.length).toBe(afterVisiblePoll);

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    fireEvent(document, new Event("visibilitychange"));

    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(afterVisiblePoll);
    });
  });

  it("shows a UIN fallback note when member nicknames are not cached yet", async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/juhe/conversations?")) {
        return jsonResponse({
          conversations: [
            {
              conversation_id: "R:2001",
              room_id: "2001",
              title: "Dev Group",
              member_count: 3,
              last_message_preview: "Latest update",
              last_message_sender_name: "Alice",
              last_message_at: 1710000200,
              has_messages: true,
            },
          ],
          total: 1,
          query: "",
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001/messages")) {
        return jsonResponse({ conversation_id: "R:2001", has_more: false, messages: [] });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001")) {
        return jsonResponse({
          conversation: {
            conversation_id: "R:2001",
            room_id: "2001",
            title: "Dev Group",
            member_count: 3,
            has_messages: true,
          },
          members: [
            { uin: "1688858038755018", nickname: "", roomname_remark: "" },
            { uin: "7881300558115752", nickname: "", roomname_remark: "" },
            { uin: "7881301849355071", nickname: "", roomname_remark: "" },
          ],
          room_memory: {
            exists: false,
            path: null,
            content: "",
          },
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    renderAppAt("/juhe");

    expect(await screen.findByText("Member Directory")).toBeInTheDocument();
    expect(await screen.findByText("Display names are not cached for this group yet. Showing member UINs.")).toBeInTheDocument();
    expect(await screen.findByText("UIN 1688858038755018")).toBeInTheDocument();
  });

  it("uses fixed-height panes so sidebars scroll independently", async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/juhe/conversations?")) {
        return jsonResponse({
          conversations: [
            {
              conversation_id: "R:2001",
              room_id: "2001",
              title: "Dev Group",
              member_count: 2,
              last_message_preview: "Latest update",
              last_message_sender_name: "Alice",
              last_message_at: 1710000200,
              has_messages: true,
            },
          ],
          total: 1,
          query: "",
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001/messages")) {
        return jsonResponse({ conversation_id: "R:2001", has_more: false, messages: [] });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001")) {
        return jsonResponse({
          conversation: {
            conversation_id: "R:2001",
            room_id: "2001",
            title: "Dev Group",
            member_count: 2,
            has_messages: true,
          },
          members: [],
          room_memory: {
            exists: false,
            path: null,
            content: "",
          },
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    renderAppAt("/juhe");

    expect(await screen.findByTestId("juhe-root")).toHaveClass("h-screen");
    expect(screen.getByTestId("juhe-workspace")).toHaveClass("min-h-0");
    expect(screen.getByTestId("juhe-info-panel")).toHaveClass("min-h-0");
    expect(screen.getByTestId("juhe-info-scroll")).toHaveClass("overflow-y-auto");
  });

  it("scrolls to the latest messages by default", async () => {
    const { getScrollTop, scrollToMock } = installScrollMock();

    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/juhe/conversations?")) {
        return jsonResponse({
          conversations: [
            {
              conversation_id: "R:2001",
              room_id: "2001",
              title: "Dev Group",
              member_count: 2,
              last_message_preview: "Newest message",
              last_message_sender_name: "Alice",
              last_message_at: 1710000200,
              has_messages: true,
            },
          ],
          total: 1,
          query: "",
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001/messages")) {
        return jsonResponse({
          conversation_id: "R:2001",
          has_more: false,
          messages: [
            {
              id: "msg-1",
              message_id: "msg-1",
              preview: "Older message",
              text: "Older message",
              is_text: true,
              message_type: 2,
              message_type_label: "文本",
              timestamp: 1710000100,
              direction: "inbound",
              sender_name: "Alice",
            },
            {
              id: "msg-2",
              message_id: "msg-2",
              preview: "Newest message",
              text: "Newest message",
              is_text: true,
              message_type: 2,
              message_type_label: "文本",
              timestamp: 1710000200,
              direction: "inbound",
              sender_name: "Bob",
            },
          ],
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001")) {
        return jsonResponse({
          conversation: {
            conversation_id: "R:2001",
            room_id: "2001",
            title: "Dev Group",
            member_count: 2,
            has_messages: true,
          },
          members: [],
          room_memory: {
            exists: false,
            path: null,
            content: "",
          },
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    renderAppAt("/juhe");

    expect(await screen.findByText("Newest message")).toBeInTheDocument();
    expect(await screen.findByTestId("juhe-message-scroll")).toBeInTheDocument();

    await waitFor(() => {
      expect(scrollToMock).toHaveBeenCalled();
      expect(getScrollTop()).toBe(800);
    });
  });

  it("preserves the current reading position on manual refresh", async () => {
    const { getScrollTop, setScrollTop } = installScrollMock();

    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/juhe/conversations?")) {
        return jsonResponse({
          conversations: [
            {
              conversation_id: "R:2001",
              room_id: "2001",
              title: "Dev Group",
              member_count: 2,
              last_message_preview: "Newest message",
              last_message_sender_name: "Alice",
              last_message_at: 1710000200,
              has_messages: true,
            },
          ],
          total: 1,
          query: "",
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001/messages")) {
        return jsonResponse({
          conversation_id: "R:2001",
          has_more: false,
          messages: [
            {
              id: "msg-1",
              message_id: "msg-1",
              preview: "Older message",
              text: "Older message",
              is_text: true,
              message_type: 2,
              message_type_label: "文本",
              timestamp: 1710000100,
              direction: "inbound",
              sender_name: "Alice",
            },
            {
              id: "msg-2",
              message_id: "msg-2",
              preview: "Newest message",
              text: "Newest message",
              is_text: true,
              message_type: 2,
              message_type_label: "文本",
              timestamp: 1710000200,
              direction: "inbound",
              sender_name: "Bob",
            },
          ],
        });
      }
      if (url.includes("/api/juhe/conversations/R%3A2001")) {
        return jsonResponse({
          conversation: {
            conversation_id: "R:2001",
            room_id: "2001",
            title: "Dev Group",
            member_count: 2,
            has_messages: true,
          },
          members: [],
          room_memory: {
            exists: false,
            path: null,
            content: "",
          },
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    renderAppAt("/juhe");

    const scroller = await screen.findByTestId("juhe-message-scroll");
    expect(await screen.findByText("Newest message")).toBeInTheDocument();

    await waitFor(() => {
      expect(getScrollTop()).toBe(800);
    });

    setScrollTop(320);
    fireEvent.scroll(scroller);
    fireEvent.click(screen.getByLabelText("Refresh conversations"));

    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(6);
      expect(getScrollTop()).toBe(320);
    });
  });
});
