import React, { useState, useEffect, useRef } from 'react';
import { X, Search } from 'lucide-react';
import { t } from '@/i18n';

interface EmojiPickerProps {
  isOpen: boolean;
  onSelect: (emoji: string) => void;
  onClose: () => void;
  position?: 'top' | 'bottom';
}

type EmojiCategory = 'recent' | 'smileys' | 'gestures' | 'hearts' | 'animals' | 'food' | 'objects';

interface EmojiGroup {
  category: EmojiCategory;
  label: string;
  emojis: string[];
}

const EMOJI_GROUPS: EmojiGroup[] = [
  {
    category: 'smileys',
    label: '😊',
    emojis: ['😀', '😃', '😄', '😁', '😆', '😅', '🤣', '😂', '😊', '😇', '🙂', '🙃', '😉', '😌', '😍', '🥰', '😘', '😗', '😚', '😙', '🥲', '😋', '😛', '😜', '🤪', '😝', '😑', '😐', '😶', '😏', '😒', '😞', '😔', '😟', '😕', '🙁', '😲', '😳', '😦', '😧', '😨', '😰', '😥', '😢', '😭', '😱', '😖', '😣', '😞', '😓', '😩', '😫', '🥱', '😤', '😡', '😠', '🤬', '😈', '👿', '💀', '☠️', '💩', '🤡', '👹', '👺', '👻', '👽', '👾', '🤖'],
  },
  {
    category: 'gestures',
    label: '👋',
    emojis: ['👋', '🤚', '🖐️', '✋', '🖖', '👌', '🤌', '🤏', '✌️', '🤞', '🫰', '🤟', '🤘', '🤙', '👍', '👎', '✊', '👊', '🤛', '🤜', '👏', '🙌', '👐', '🤲', '🤝', '🤜', '🤛', '🦵', '🦶'],
  },
  {
    category: 'hearts',
    label: '❤️',
    emojis: ['❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍', '🤎', '💔', '💕', '💞', '💓', '💗', '💖', '💘', '💝', '💟'],
  },
  {
    category: 'animals',
    label: '🐶',
    emojis: ['🐶', '🐱', '🐭', '🐹', '🐰', '🦊', '🐻', '🐼', '🐨', '🐯', '🦁', '🐮', '🐷', '🐸', '🐵', '🐔', '🐧', '🐦', '🐤', '🦆', '🦅', '🦉', '🦇', '🐺', '🐗', '🐴', '🦄', '🐝', '🪱', '🐛', '🦋', '🐌', '🐞', '🐜', '🪰', '🐢', '🐍', '🐙', '🦑', '🦐', '🦞', '🦀', '🐡', '🐠', '🐟', '🐬', '🐳', '🐋'],
  },
  {
    category: 'food',
    label: '🍎',
    emojis: ['🍎', '🍊', '🍋', '🍌', '🍉', '🍇', '🍓', '🍈', '🍒', '🍑', '🥭', '🍍', '🥥', '🥝', '🍅', '🍆', '🥑', '🥦', '🥬', '🥒', '🌶️', '🌽', '🥕', '🧄', '🧅', '🥔', '🍞', '🥐', '🥯', '🍠', '🥐', '🍳', '🧈', '🥞', '🧇', '🥓', '🍗', '🍖', '🌭', '🍔', '🍟', '🍕', '🥪', '🥙', '🧆', '🌮', '🌯', '🥗', '🥘', '🥫', '🍝', '🍜', '🍲', '🍛', '🍣', '🍱', '🥟', '🦪', '🍤', '🍙', '🍚', '🍘', '🍥', '🥠', '🥮', '🍢', '🍡', '🍧', '🍨', '🍦', '🍰', '🎂', '🧁', '🍮', '🍭', '🍬', '🍫', '🍿', '🍩', '🍪', '🌰', '🍯', '🥛', '🍼', '☕', '🍵', '🍶', '🍾', '🍷', '🍸', '🍹', '🍺', '🍻', '🥂', '🥃'],
  },
  {
    category: 'objects',
    label: '⚽',
    emojis: ['⚽', '🏀', '🏈', '⚾', '🥎', '🎾', '🏐', '🏉', '🥏', '🎳', '🏓', '🏸', '🏒', '🏑', '🥍', '🏏', '🥅', '⛳', '⛸️', '🎣', '🎽', '🎿', '⛷️', '🏂', '🪂', '🛼', '🛹', '🛼', '🛴', '⛸️', '🛰️', '🚗', '🚕', '🚙', '🚌', '🚎', '🏎️', '🚓', '🚑', '🚒', '🚐', '🛻', '🚚', '🚛', '🚜', '🏍️', '🏎️', '🛵', '🦯', '🦽', '🦼', '🛺', '🚲', '🛴', '🛹', '🛼', '🚨', '🚔', '🚍', '🚘', '🚖', '🚡', '🚠', '🚟', '🚃', '🚋', '🚞', '🚝', '🚄', '🚅', '🚈', '🚂', '🚆', '🚇', '🚊', '🚉', '✈️', '🛫', '🛬', '🛰️', '🚁', '🛶', '⛵', '🚤', '🛳️', '⛴️', '🛥️', '🛳️', '🚢'],
  },
];

const RECENT_STORAGE_KEY = 'commclient_recent_emojis';
const RECENT_MAX = 20;

export const EmojiPicker: React.FC<EmojiPickerProps> = ({
  isOpen,
  onSelect,
  onClose,
  position = 'bottom',
}) => {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeCategory, setActiveCategory] = useState<EmojiCategory>('recent');
  const [recentEmojis, setRecentEmojis] = useState<string[]>([]);
  const pickerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // Load recent emojis from localStorage
  useEffect(() => {
    try {
      const stored = localStorage.getItem(RECENT_STORAGE_KEY);
      if (stored) {
        setRecentEmojis(JSON.parse(stored));
      }
    } catch {
      // Ignore localStorage errors
    }
  }, []);

  // Handle click outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        onClose();
      }
    };

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };

    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      document.addEventListener('keydown', handleEscape);
      searchInputRef.current?.focus();
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [isOpen, onClose]);

  const handleEmojiSelect = (emoji: string) => {
    onSelect(emoji);

    // Update recent emojis
    const updated = [emoji, ...recentEmojis.filter((e) => e !== emoji)].slice(0, RECENT_MAX);
    setRecentEmojis(updated);
    try {
      localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(updated));
    } catch {
      // Ignore localStorage errors
    }
  };

  const getFilteredEmojis = (): string[] => {
    if (!searchQuery.trim()) {
      const group = EMOJI_GROUPS.find((g) => g.category === activeCategory);
      if (activeCategory === 'recent') {
        return recentEmojis;
      }
      return group?.emojis || [];
    }

    const query = searchQuery.toLowerCase();
    const allEmojis = EMOJI_GROUPS.flatMap((g) => g.emojis);
    return allEmojis.filter((emoji) => {
      const codePoint = emoji.codePointAt(0);
      if (!codePoint) return false;
      const name = getEmojiName(emoji);
      return name.includes(query);
    });
  };

  const getEmojiName = (emoji: string): string => {
    const names: Record<string, string> = {
      '😀': 'grinning face',
      '😃': 'grinning face with big eyes',
      '😄': 'grinning face with smiling eyes',
      '❤️': 'red heart',
      '🔥': 'fire',
      '👍': 'thumbs up',
      '👎': 'thumbs down',
      '🎉': 'party popper',
      '🎊': 'confetti ball',
      '👋': 'waving hand',
      '🙌': 'raising hands',
      '😂': 'joy',
      '💔': 'broken heart',
      '✨': 'sparkles',
    };
    return names[emoji] || '';
  };

  if (!isOpen) return null;

  const filteredEmojis = getFilteredEmojis();
  const positionClasses = position === 'top' ? 'bottom-full mb-2' : 'top-full mt-2';

  return (
    <div
      ref={pickerRef}
      className={`absolute ${positionClasses} left-0 right-0 w-80 bg-surface-900 border border-surface-700 rounded-lg shadow-2xl z-50`}
      role="dialog"
      aria-label="Emoji picker"
    >
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-surface-700">
        <h3 className="text-sm font-semibold text-white">Emoji</h3>
        <button
          onClick={onClose}
          className="text-surface-400 hover:text-white transition-colors p-1 -m-1"
          aria-label="Close emoji picker"
        >
          <X size={18} />
        </button>
      </div>

      {/* Search */}
      <div className="p-3 border-b border-surface-700">
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 transform -translate-y-1/2 text-surface-500" />
          <input
            ref={searchInputRef}
            type="text"
            placeholder="Search emoji..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-8 pr-3 py-2 bg-surface-800 border border-surface-700 rounded text-white text-sm placeholder-surface-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
            aria-label="Search emojis"
          />
        </div>
      </div>

      {/* Categories */}
      {!searchQuery && (
        <div className="flex gap-1 p-2 border-b border-surface-700 overflow-x-auto">
          {[{ cat: 'recent' as const, label: '🕐' }, ...EMOJI_GROUPS.slice(0, 6).map((g) => ({ cat: g.category, label: g.label }))].map(
            ({ cat, label }) => (
              <button
                key={cat}
                onClick={() => setActiveCategory(cat)}
                className={`flex-shrink-0 w-8 h-8 rounded flex items-center justify-center text-lg transition-colors ${
                  activeCategory === cat ? 'bg-primary-500' : 'bg-surface-800 hover:bg-surface-700'
                }`}
                aria-pressed={activeCategory === cat}
                title={cat}
              >
                {label}
              </button>
            )
          )}
        </div>
      )}

      {/* Emoji grid */}
      <div className="p-3 max-h-60 overflow-y-auto">
        {filteredEmojis.length > 0 ? (
          <div className="grid grid-cols-8 gap-1">
            {filteredEmojis.map((emoji) => (
              <button
                key={emoji}
                onClick={() => handleEmojiSelect(emoji)}
                className="aspect-square flex items-center justify-center text-xl hover:bg-surface-700 rounded transition-colors"
                title={getEmojiName(emoji)}
                aria-label={getEmojiName(emoji)}
              >
                {emoji}
              </button>
            ))}
          </div>
        ) : (
          <div className="text-center py-8 text-surface-500 text-sm">
            {searchQuery ? 'No emojis found' : 'No recent emojis'}
          </div>
        )}
      </div>
    </div>
  );
};
