import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';

import { requestAPI } from './handler';

/**
 * Initialization data for the jupyter-server-nbmodel extension.
 */
const plugin: JupyterFrontEndPlugin<void> = {
  id: 'jupyter-server-nbmodel:plugin',
  description: 'A Jupyter Server extension to execute code cell from the server.',
  autoStart: true,
  activate: (app: JupyterFrontEnd) => {
    console.log('JupyterLab extension jupyter-server-nbmodel is activated!');

    requestAPI<any>('get-example')
      .then(data => {
        console.log(data);
      })
      .catch(reason => {
        console.error(
          `The jupyter_server_nbmodel server extension appears to be missing.\n${reason}`
        );
      });
  }
};

export default plugin;
