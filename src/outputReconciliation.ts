/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import type { CodeCell, ICodeCellModel } from '@jupyterlab/cells';
import { JSONExt } from '@lumino/coreutils';

/**
 * Parse server outputs and apply JupyterLab's consecutive stream merge rule.
 */
export function normalizeServerOutputs(outputs: string | unknown[]): any[] {
  const rawOutputs = (
    typeof outputs === 'string' ? JSON.parse(outputs) : outputs
  ) as any[];
  return rawOutputs.reduce<any[]>((merged, output) => {
    const previous = merged[merged.length - 1];
    if (
      output.output_type === 'stream' &&
      previous?.output_type === 'stream' &&
      previous.name === output.name
    ) {
      const previousText = Array.isArray(previous.text)
        ? previous.text.join('')
        : (previous.text ?? '');
      const text = Array.isArray(output.text)
        ? output.text.join('')
        : (output.text ?? '');
      previous.text = previousText + text;
    } else {
      merged.push(output);
    }
    return merged;
  }, []);
}

function streamText(output: any): string {
  return Array.isArray(output.text)
    ? output.text.join('')
    : (output.text ?? '');
}

function isStreamPrefix(prefix: any, output: any): boolean {
  return (
    prefix?.output_type === 'stream' &&
    output?.output_type === 'stream' &&
    prefix.name === output.name &&
    streamText(output).startsWith(streamText(prefix))
  );
}

/**
 * Whether every output in `prefix` is present at the beginning of `outputs`.
 * The final stream may contain a text prefix rather than the complete text.
 */
export function isOutputPrefix(prefix: any[], outputs: any[]): boolean {
  if (prefix.length > outputs.length) {
    return false;
  }
  return prefix.every((output, index) => {
    return (
      JSONExt.deepEqual(output, outputs[index]) ||
      (index === prefix.length - 1 && isStreamPrefix(output, outputs[index]))
    );
  });
}

/**
 * Append the part of a server snapshot missing from the output-area model.
 * Using the output-area API preserves the existing stream widget and lets the
 * cell model translate the append into a Y.Text insertion.
 */
function appendOutputSuffix(
  cell: CodeCell,
  currentOutputs: any[],
  serverOutputs: any[]
): void {
  let nextOutput = currentOutputs.length;
  if (currentOutputs.length > 0) {
    const lastIndex = currentOutputs.length - 1;
    const current = currentOutputs[lastIndex];
    const server = serverOutputs[lastIndex];
    if (!JSONExt.deepEqual(current, server)) {
      const text = streamText(server).slice(streamText(current).length);
      if (text) {
        cell.outputArea.model.add({ ...server, text });
      }
    }
  }

  while (nextOutput < serverOutputs.length) {
    cell.outputArea.model.add(serverOutputs[nextOutput]);
    nextOutput += 1;
  }
}

/**
 * Reconcile one pending output snapshot without allowing an older snapshot to
 * replace newer collaborative output.
 */
export function reconcileOutputSnapshot(
  cell: CodeCell,
  serverOutputs: any[],
  previousServerOutputs?: any[]
): void {
  const sharedCodeCell = (cell.model as ICodeCellModel).sharedModel;
  const currentOutputs = sharedCodeCell.getOutputs();
  if (JSONExt.deepEqual(currentOutputs, serverOutputs)) {
    return;
  }

  // An initial empty snapshot can arrive after a collaborative output update.
  // Never let that older snapshot erase output already visible in the cell.
  // A transition from non-empty to empty, however, represents clear_output.
  if (serverOutputs.length === 0) {
    if (previousServerOutputs?.length && currentOutputs.length) {
      cell.outputArea.model.clear();
    }
    return;
  }

  // The collaborative path may be ahead of this HTTP response. Applying the
  // older snapshot would make output disappear and then reappear on the next
  // poll, so leave the newer model untouched.
  if (isOutputPrefix(serverOutputs, currentOutputs)) {
    return;
  }

  console.debug('[jupyter-server-nbmodel] Applying pending output snapshot', {
    cellId: sharedCodeCell.getId(),
    outputCount: serverOutputs.length
  });
  if (isOutputPrefix(currentOutputs, serverOutputs)) {
    appendOutputSuffix(cell, currentOutputs, serverOutputs);
  } else {
    // Non-monotonic output changes (for example update_display_data) cannot be
    // represented as an append and still require authoritative replacement.
    sharedCodeCell.setOutputs(serverOutputs);
  }
}
