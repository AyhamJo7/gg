import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { RunInspector } from "../components/RunInspector";
import { UsageValue, EstimateValue } from "../components/UsageValue";

const mockGet = vi.fn();

vi.mock("../lib/api", () => ({
  api: {
    runs: () => ({
      get: mockGet,
    }),
  },
}));

import { api } from "../lib/api";

void api;

describe("UsageValue", () => {
  it("renders Unknown for null instead of 0", () => {
    render(<UsageValue value={null} label="input" />);
    expect(screen.getByText("Unknown")).toBeDefined();
  });

  it("renders numbers with locale grouping", () => {
    render(<UsageValue value={1234} label="input" />);
    expect(screen.getByText("1,234")).toBeDefined();
  });

  it("labels estimates explicitly", () => {
    render(<EstimateValue tokens={3800} />);
    expect(screen.getByText("~3.8k estimated")).toBeDefined();
  });
});

describe("RunInspector", () => {
  beforeEach(() => {
    mockGet.mockReset();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows unknown usage honestly for AGY", async () => {
    mockGet.mockResolvedValue({
      run: {
        id: "run1",
        provider: "agy",
        role: "implementation",
        failure_class: "NONE",
        provider_state: "COMPLETED",
        exit_code: 0,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        summary: "",
      },
      context: {
        run_id: "run1",
        prompt_chars: 4000,
        prompt_bytes: 4000,
        prompt_words: 500,
        estimated_prompt_tokens: 1000,
        estimator_id: "char4-v1",
        prompt_hash: "abc",
        capture_status: "CAPTURED",
      },
      usage: {
        run_id: "run1",
        input_tokens_total: null,
        output_tokens_total: null,
        cache_read_input_tokens: null,
        cache_write_input_tokens: null,
        reasoning_output_tokens: null,
        native_total_tokens: null,
        source: "UNKNOWN",
        completeness: "UNKNOWN",
        observed_model: null,
        requested_model: null,
      },
    });
    render(<RunInspector runId="run1" onClose={() => undefined} />);
    await waitFor(() => expect(screen.getByText(/AGY usage unknown/)).toBeDefined());
  });
});
