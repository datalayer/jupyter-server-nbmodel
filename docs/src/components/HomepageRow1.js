/*
 * Copyright (c) 2025-2026 Datalayer, Inc.
 * Distributed under the terms of the Modified BSD License.
 */

import React from 'react';
import clsx from 'clsx';
import styles from './HomepageRow1.module.css';

const items = [
  {
    title: '100% compatible with Jupyter',
    Svg: require('../../static/img/ringed-planet.svg').default,
    description: (
      <>
        Datalayer is 💯% compatible with Jupyter standards and protocols.
      </>
    ),
  },
  {
    title: 'Cloud-native',
    Svg: require('../../static/img/cloud-sun.svg').default,
    description: (
      <>
        Get started by creating a Jupyter platform with Kubernetes with storage and kernel processing in the cloud.
      </>
    ),
  },
  {
    title: 'Web friendly with React.js',
    Svg: require('../../static/img/react-js2.svg').default,
    description: (
      <>
        If you are sick with the JupyterLab frontend limits and want to leverage the React.js developers and developement eco-system, we have something for you.
      </>
    ),
  },
];

function Product({Svg, title, description}) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center">
        <Svg className={styles.productSvg} alt={title} />
      </div>
      <div className="text--center padding-horiz--md">
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HompageRow1() {
  return (
    <section className={styles.Products}>
      <div className="container">
        <div className="row">
          {items.map((props, idx) => (
            <Product key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
