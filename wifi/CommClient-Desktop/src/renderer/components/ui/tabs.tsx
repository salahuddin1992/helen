import React, { createContext, useContext, ReactNode } from 'react';

interface TabsContextType {
  value: string;
  onValueChange: (value: string) => void;
}

const TabsContext = createContext<TabsContextType | undefined>(undefined);

export interface TabsProps {
  value: string;
  onValueChange: (value: string) => void;
  children: ReactNode;
  className?: string;
}

export const Tabs: React.FC<TabsProps> = ({
  value,
  onValueChange,
  children,
  className = '',
}) => {
  return (
    <TabsContext.Provider value={{ value, onValueChange }}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  );
};

export interface TabsListProps {
  children: ReactNode;
  className?: string;
}

export const TabsList: React.FC<TabsListProps> = ({ children, className = '' }) => {
  return <div className={className}>{children}</div>;
};

export interface TabsTriggerProps {
  value: string;
  children: ReactNode;
  className?: string;
}

export const TabsTrigger: React.FC<TabsTriggerProps> = ({
  value,
  children,
  className = '',
}) => {
  const context = useContext(TabsContext);
  if (!context) {
    throw new Error('TabsTrigger must be used within Tabs');
  }

  const { value: activeValue, onValueChange } = context;
  const isActive = activeValue === value;

  return (
    <button
      onClick={() => onValueChange(value)}
      className={className}
      data-state={isActive ? 'active' : 'inactive'}
      role="tab"
      aria-selected={isActive}
    >
      {children}
    </button>
  );
};

export interface TabsContentProps {
  value: string;
  children: ReactNode;
  className?: string;
}

export const TabsContent: React.FC<TabsContentProps> = ({
  value,
  children,
  className = '',
}) => {
  const context = useContext(TabsContext);
  if (!context) {
    throw new Error('TabsContent must be used within Tabs');
  }

  const { value: activeValue } = context;
  const isActive = activeValue === value;

  if (!isActive) {
    return null;
  }

  return <div className={className}>{children}</div>;
};
