// SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
// SPDX-FileCopyrightText: 2026 Kaushik <kaushikrjpm10@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { useState, useCallback, useEffect, useMemo } from "react";
import { Check, Copy, Terminal } from "lucide-react";
import { toast } from "sonner";
import { copyToClipboard } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { PickerSelect } from "@/components/ui/picker-select";
import { useHarnesses } from "@/hooks/use-harnesses";

interface ComponentInstallCommandProps {
  componentType: string;
  componentName: string;
}

export function ComponentInstallCommand({ componentType, componentName }: ComponentInstallCommandProps) {
  const { data: harnesses, defaultHarness } = useHarnesses();
  const requiredCapability = componentType === "mcp" ? "mcp_servers" : `${componentType}s`;
  const supportedHarnesses = useMemo(
    () => (harnesses ?? []).filter((entry) => entry.capabilities.includes(requiredCapability)),
    [harnesses, requiredCapability],
  );
  const [harness, setHarness] = useState("");
  useEffect(() => {
    if (supportedHarnesses.length === 0) return;
    if (!supportedHarnesses.some((entry) => entry.name === harness)) {
      setHarness(supportedHarnesses.find((entry) => entry.name === defaultHarness)?.name ?? supportedHarnesses[0].name);
    }
  }, [supportedHarnesses, defaultHarness, harness]);
  const [copied, setCopied] = useState(false);

  const effectiveHarness = supportedHarnesses.some((entry) => entry.name === harness)
    ? harness : (supportedHarnesses.find((entry) => entry.name === defaultHarness)?.name ?? supportedHarnesses[0]?.name);
  const command = effectiveHarness
    ? `observal registry ${componentType} install ${componentName} --harness ${effectiveHarness}`
    : null;

  const handleCopy = useCallback(async () => {
    if (!command) return;
    try {
      await copyToClipboard(command);
      setCopied(true);
      toast.success("Copied to clipboard");
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Failed to copy");
    }
  }, [command]);

  return (
    <div className="border border-border rounded-md bg-surface-sunken">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <Terminal className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-xs font-medium text-muted-foreground">Install</span>
        <div className="ml-auto">
          <PickerSelect
            value={effectiveHarness ?? ""}
            onValueChange={setHarness}
            className="w-[130px]"
            inputClassName="h-7 border-border text-xs"
            options={supportedHarnesses.map((entry) => ({ value: entry.name, label: entry.display_name }))}
          />
        </div>
      </div>
      <div className="flex items-center gap-2 p-3">
        <code className="min-w-0 flex-1 break-all text-sm font-mono select-all text-foreground leading-relaxed">
          {command ? <><span className="text-muted-foreground">$</span> {command}</> : "No supported harness available"}
        </code>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0 hover:bg-accent"
          onClick={handleCopy}
          disabled={!command}
          aria-label="Copy command"
        >
          {copied ? (
            <Check className="h-3.5 w-3.5 text-success" />
          ) : (
            <Copy className="h-3.5 w-3.5" />
          )}
        </Button>
      </div>
    </div>
  );
}
