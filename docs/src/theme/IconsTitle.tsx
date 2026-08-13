/*
 * Copyright (c) 2025-2026 Datalayer, Inc.
 * Distributed under the terms of the Modified BSD License.
 */

import React from 'react';

import '@primer/react-brand/lib/css/main.css';

type Props = {
  icon1: JSX.Element;
  icon2: JSX.Element;
  title: string;
};

export const IconsTitle = (props: Props) => {
  const { icon1, icon2, title } = props;
  return (
    <div style={{ display: 'flex' }}>
      <div>{icon1}</div>
      <div>{icon2}</div>
      <div>{title}</div>
    </div>
  );
};

export default IconsTitle;
