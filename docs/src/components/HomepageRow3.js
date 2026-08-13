/*
 * Copyright (c) 2025-2026 Datalayer, Inc.
 * Distributed under the terms of the Modified BSD License.
 */

import React from 'react';
import clsx from 'clsx';
import styles from './HomepageRow3.module.css';

const items = [
  {
    title: 'Flexible Deployment',
    Svg: require('../../static/img/rocket.svg').default,
    description: (
      <>
        Deploy on-premises, in your own cloud, or fully whitelabelled — Datalayer adapts to your infrastructure and branding needs.
      </>
    ),
  },
  {
    title: 'Open source',
    Svg: require('../../static/img/open-source.svg').default,
    description: (
      <>
        Extend or customize your platform to your needs. Sponsoring and support options are available if you need them.
      </>
    ),
  },
  {
    title: 'Supported',
    Svg: require('../../static/img/thumbs-up.svg').default,
    description: (
      <>
        Stay cool, we offer a range of <a href="/professional-services">professional services</a>.
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

export default function HomepageRow3() {
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
