import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RunInspector } from "../components/RunInspector";
import { UsageValue, EstimateValue } from "../components/UsageValue";

const mockGet = vi.fn();
const mockContext = vi.fn();

vi.mock("../lib/api", () => ({
  api: {
    runs: () => ({
      get: mockGet,
      context: mockContext,
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
    mockContext.mockReset();
    mockContext.mockResolvedValue({ run_id: "run1", blocks: [], warnings: [] });
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

  it("reveals compiled context blocks without raw prompts", async () => {
    mockGet.mockResolvedValue({
      run: {
        id: "run2",
        provider: "opencode",
        role: "implementation",
        failure_class: "NONE",
        provider_state: "COMPLETED",
        exit_code: 0,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        summary: "",
        prompt_template_version: "compiled-v2",
        context_policy_version: "context-policy-v2",
      },
      context: {
        run_id: "run2",
        prompt_chars: 1200,
        prompt_bytes: 1200,
        prompt_words: 200,
        estimated_prompt_tokens: 300,
        estimator_id: "char4-v1",
        prompt_hash: "abc",
        capture_status: "CAPTURED",
        budget_estimated_tokens: 16000,
        used_estimated_tokens: 300,
        remaining_estimated_tokens: 15700,
        repeated_context_ratio: 0.125,
        warnings: ["MODEL_CONTEXT_LIMIT_UNKNOWN"],
      },
      usage: {
        run_id: "run2",
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
    mockContext.mockResolvedValue({
      run_id: "run2",
      blocks: [
        {
          block_type: "ACCEPTANCE_CRITERION",
          block_id: "acc-R4-A1",
          source_kind: "plan",
          source_ref: "criterion:R4-A1",
          priority: "MANDATORY",
          original_chars: 100,
          included_chars: 100,
          estimated_tokens: 25,
          representation: "FULL",
          included: true,
          reason: "included",
          hash: "deadbeef",
        },
      ],
      warnings: ["MODEL_CONTEXT_LIMIT_UNKNOWN"],
    });
    render(<RunInspector runId="run2" onClose={() => undefined} />);
    await waitFor(() => expect(screen.getByText("View Context")).toBeDefined());
    fireEvent.click(screen.getByText("View Context"));
    await waitFor(() => expect(screen.getByText(/ACCEPTANCE_CRITERION/)).toBeDefined());
    expect(screen.getByText(/MANDATORY · FULL/)).toBeDefined();
  });
});
