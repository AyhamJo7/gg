import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/auth", () => ({
  isTauri: false,
  getAuthToken: vi.fn(),
}));

import { getAuthToken } from "../lib/auth";
import { api } from "../lib/api";

const mockedGetAuthToken = vi.mocked(getAuthToken);

function stubFetch(body: unknown = {}, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("api client bearer-token attachment", () => {
  beforeEach(() => {
    mockedGetAuthToken.mockReset();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("attaches Authorization on a mutating (POST) request when a token is available", async () => {
    mockedGetAuthToken.mockResolvedValue("test-token-abc");
    const fetchMock = stubFetch({ id: "p1" });

    await api.projects.add("/some/path");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer test-token-abc");
  });

  it("does not attach Authorization on a GET request even when a token is available", async () => {
    mockedGetAuthToken.mockResolvedValue("test-token-abc");
    const fetchMock = stubFetch([]);

    await api.projects.list();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    // getAuthToken must not even be consulted for GETs.
    expect(mockedGetAuthToken).not.toHaveBeenCalled();
  });

  it("omits Authorization on a mutating request when no token is available", async () => {
    mockedGetAuthToken.mockResolvedValue(null);
    const fetchMock = stubFetch({ id: "p1" });

    await api.projects.add("/some/path");

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });
});
