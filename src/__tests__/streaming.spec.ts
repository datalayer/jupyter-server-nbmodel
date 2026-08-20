/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

/**
 * A cell shows a stream WHILE it runs, not only once it ends.
 *
 * The snapshots below are what the server actually answers while a loop
 * prints: `/api/kernels/<id>/requests/<uid>` replies `202` with the outputs
 * so far, one stream message per `print`, until the last reply carries the
 * whole run. The page applies each of them as it arrives — that is the
 * streaming — and a bug that only shows up here leaves the cell empty until
 * the execution ends, when the final answer lands everything at once.
 */

import type { CodeCell } from '@jupyterlab/cells';
import {
  normalizeServerOutputs,
  reconcileOutputSnapshot
} from '../outputReconciliation';

/**
 * A cell whose outputs LIVE: what is added is readable back, and two
 * consecutive streams of one name merge, as JupyterLab's output area model
 * does. The stubs elsewhere answer a fixed list, which cannot show a cell
 * filling up over several snapshots.
 */
function createLiveCell(): { cell: CodeCell; text: () => string } {
  const outputs: any[] = [];
  const add = (output: any) => {
    const previous = outputs[outputs.length - 1];
    if (
      output.output_type === 'stream' &&
      previous?.output_type === 'stream' &&
      previous.name === output.name
    ) {
      previous.text = `${previous.text ?? ''}${output.text ?? ''}`;
      return;
    }
    outputs.push({ ...output });
  };
  return {
    cell: {
      model: {
        sharedModel: {
          getId: () => 'cell-id',
          getOutputs: () => outputs,
          setOutputs: (next: any[]) => {
            outputs.length = 0;
            next.forEach(output => outputs.push({ ...output }));
          }
        }
      },
      outputArea: {
        model: { add, clear: () => (outputs.length = 0) }
      }
    } as unknown as CodeCell,
    text: () =>
      outputs
        .filter(output => output.output_type === 'stream')
        .map(output => output.text)
        .join('')
  };
}

/** What the server answers after `count` of the prints have run. */
function snapshotAfter(count: number): string {
  return JSON.stringify(
    Array.from({ length: count }, (_, index) => ({
      output_type: 'stream',
      name: 'stdout',
      text: `tick ${index}\n`
    }))
  );
}

describe('streaming a running cell', () => {
  it('grows the cell with every snapshot of the execution', () => {
    const { cell, text } = createLiveCell();
    let previous: any[] | undefined;

    const seen: string[] = [];
    for (let count = 1; count <= 4; count += 1) {
      // Exactly what `reconcilePendingOutputs` does with a pending answer.
      const serverOutputs = normalizeServerOutputs(snapshotAfter(count));
      reconcileOutputSnapshot(cell, serverOutputs, previous);
      previous = serverOutputs;
      seen.push(text());
    }

    expect(seen).toEqual([
      'tick 0\n',
      'tick 0\ntick 1\n',
      'tick 0\ntick 1\ntick 2\n',
      'tick 0\ntick 1\ntick 2\ntick 3\n'
    ]);
  });

  it('writes each print once, however often the same snapshot arrives', () => {
    const { cell, text } = createLiveCell();
    const serverOutputs = normalizeServerOutputs(snapshotAfter(2));

    reconcileOutputSnapshot(cell, serverOutputs, undefined);
    reconcileOutputSnapshot(cell, serverOutputs, serverOutputs);
    reconcileOutputSnapshot(cell, serverOutputs, serverOutputs);

    expect(text()).toBe('tick 0\ntick 1\n');
  });
});
