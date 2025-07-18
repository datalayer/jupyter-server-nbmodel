/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { INotebookCellExecutor } from '@jupyterlab/notebook';
import { NotebookCellServerExecutor } from './executor';

export const notebookCellExecutor: JupyterFrontEndPlugin<INotebookCellExecutor> =
  {
    id: '@datalayer/jupyter-server-nbmodel:notebook-cell-executor',
    description:
      'Add notebook cell executor that uses REST API instead of kernel protocol over WebSocket.',
    autoStart: true,
    provides: INotebookCellExecutor,
    activate: (app: JupyterFrontEnd): INotebookCellExecutor => {
      const executor = new NotebookCellServerExecutor({
        serverSettings: app.serviceManager.serverSettings
      });
      console.log('JupyterLab extension jupyter-server-nbmodel is activated!');
      return executor;
    }
  };
