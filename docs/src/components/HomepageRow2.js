/*
 * Copyright (c) 2025-2026 Datalayer, Inc.
 * Distributed under the terms of the Modified BSD License.
 */

import React from 'react';
import clsx from 'clsx';
import styles from './HomepageRow2.module.css';

const items = [
  {
    title: 'Built for Scalable AI',
    Svg: require('../../static/img/sparkles.svg').default,
    description: (
      <>
        Deliver faster your AI projects. If you need more batteries for AI, have a look to our managed components with authentication, authorization, server and Jupyter kernel instant start...
      </>
    ),
  },
  {
    title: 'Components with a Storybook',
    Svg: require('../../static/img/building-construction.svg').default,
    description: (
      <>
        You build your custom Data Product with well crafted components. Have a look at the <a href="https://storybook.datalayer.tech" target="_blank">Storybook</a>.
      </>
    ),
  },
  {
    title: 'Trusted',
    Svg: require('../../static/img/shield.svg').default,
    description: (
      <>
        We are working towards entreprise-grade security certifications - ISO 27001, SOC2 type 2, GDPR and ISO 42001.
      </>
    ),
  },
];

function Feature({Svg, title, description}) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center">
        <Svg className={styles.featureSvg} alt={title} />
      </div>
      <div className="text--center padding-horiz--md">
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageRow2() {
  return (
    <section className={styles.implementations}>
      <div className="container">
        <div className="row">
          {items.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
