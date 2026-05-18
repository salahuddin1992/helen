import React, { useState, useEffect, useCallback } from 'react';
import { Search, X } from 'lucide-react';

interface SearchBarProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
  autoFocus?: boolean;
  debounceMs?: number;
}

export const SearchBar: React.FC<SearchBarProps> = ({
  value,
  onChange,
  placeholder = 'Search...',
  className = '',
  autoFocus = false,
  debounceMs = 300,
}) => {
  const [localValue, setLocalValue] = useState(value);

  // Debounced onChange
  useEffect(() => {
    const timer = setTimeout(() => {
      if (localValue !== value) {
        onChange(localValue);
      }
    }, debounceMs);

    return () => clearTimeout(timer);
  }, [localValue, value, onChange, debounceMs]);

  // Update local value when prop changes
  useEffect(() => {
    setLocalValue(value);
  }, [value]);

  const handleClear = useCallback(() => {
    setLocalValue('');
    onChange('');
  }, [onChange]);

  return (
    <div className={`relative flex items-center ${className}`}>
      <Search size={18} className="absolute left-3 text-surface-500 pointer-events-none" />
      <input
        type="text"
        value={localValue}
        onChange={(e) => setLocalValue(e.target.value)}
        placeholder={placeholder}
        autoFocus={autoFocus}
        className="w-full pl-9 pr-9 py-2 bg-surface-700 text-white text-sm rounded-lg placeholder-surface-500 border border-transparent focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 transition-colors"
        aria-label="Search"
      />
      {localValue && (
        <button
          onClick={handleClear}
          className="absolute right-3 text-surface-500 hover:text-white transition-colors p-1 -m-1"
          aria-label="Clear search"
        >
          <X size={16} />
        </button>
      )}
    </div>
  );
};
