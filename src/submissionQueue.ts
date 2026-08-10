/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

/**
 * Serialize request submission per kernel.
 *
 * A turn is released as soon as the server accepts the request; execution and
 * output polling can continue while the next request is submitted.
 */
export class KernelSubmissionQueue {
  private _tails = new Map<string, Promise<void>>();

  async acquire(kernelId: string): Promise<() => void> {
    const previous = this._tails.get(kernelId) ?? Promise.resolve();
    let resolveGate: () => void = () => undefined;
    const gate = new Promise<void>(resolve => {
      resolveGate = resolve;
    });
    const tail = previous.catch(() => undefined).then(() => gate);
    this._tails.set(kernelId, tail);
    await previous.catch(() => undefined);

    let released = false;
    return () => {
      if (released) {
        return;
      }
      released = true;
      resolveGate();
      void tail.then(() => {
        if (this._tails.get(kernelId) === tail) {
          this._tails.delete(kernelId);
        }
      });
    };
  }
}
