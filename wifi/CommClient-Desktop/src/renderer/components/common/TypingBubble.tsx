import React, { useEffect, useState } from 'react';

interface TypingBubbleProps {
  users: string[];
  compact?: boolean;
}

export const TypingBubble: React.FC<TypingBubbleProps> = ({ users, compact = false }) => {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    if (users.length === 0) {
      setVisible(false);
      return;
    }

    setVisible(true);
    const timer = setTimeout(() => {
      setVisible(false);
    }, 5000);

    return () => clearTimeout(timer);
  }, [users]);

  if (users.length === 0 || !visible) {
    return null;
  }

  const displayText = (() => {
    if (users.length === 1) {
      return `${users[0]} is typing`;
    }
    if (users.length === 2) {
      return `${users[0]}, ${users[1]} are typing`;
    }
    return `${users.slice(0, 2).join(', ')}, and ${users.length - 2} more are typing`;
  })();

  if (compact) {
    return (
      <div className="flex items-center gap-2 text-xs text-surface-400">
        <div className="flex gap-1">
          <span className="w-1.5 h-1.5 bg-surface-500 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
          <span className="w-1.5 h-1.5 bg-surface-500 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
          <span className="w-1.5 h-1.5 bg-surface-500 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-3 px-4 py-3 bg-surface-800/50 rounded-lg">
      <div className="flex gap-1">
        <span className="w-2 h-2 bg-surface-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
        <span className="w-2 h-2 bg-surface-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
        <span className="w-2 h-2 bg-surface-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
      </div>
      <span className="text-sm text-surface-300 font-medium">{displayText}...</span>
    </div>
  );
};
