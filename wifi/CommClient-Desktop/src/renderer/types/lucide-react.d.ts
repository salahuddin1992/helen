/**
 * Type declaration for lucide-react.
 * The installed version ships only CJS, so TS needs a module declaration.
 */
declare module 'lucide-react' {
  import { FC, SVGAttributes } from 'react';

  interface IconProps extends SVGAttributes<SVGElement> {
    size?: number | string;
    color?: string;
    strokeWidth?: number | string;
    absoluteStrokeWidth?: boolean;
    className?: string;
  }

  type Icon = FC<IconProps>;

  // Export all icons used in the project
  export const AlertTriangle: Icon;
  export const Archive: Icon;
  export const ArrowLeft: Icon;
  export const ArrowRight: Icon;
  export const AtSign: Icon;
  export const Bell: Icon;
  export const BellOff: Icon;
  export const Camera: Icon;
  export const CameraOff: Icon;
  export const Check: Icon;
  export const CheckCircle: Icon;
  export const ChevronDown: Icon;
  export const ChevronLeft: Icon;
  export const ChevronRight: Icon;
  export const ChevronUp: Icon;
  export const Clock: Icon;
  export const Copy: Icon;
  export const Download: Icon;
  export const Edit: Icon;
  export const ExternalLink: Icon;
  export const Eye: Icon;
  export const EyeOff: Icon;
  export const File: Icon;
  export const FileText: Icon;
  export const Filter: Icon;
  export const Globe: Icon;
  export const Hash: Icon;
  export const Heart: Icon;
  export const HelpCircle: Icon;
  export const Home: Icon;
  export const Image: Icon;
  export const Info: Icon;
  export const Loader: Icon;
  export const Loader2: Icon;
  export const Lock: Icon;
  export const LogOut: Icon;
  export const Mail: Icon;
  export const Menu: Icon;
  export const MessageCircle: Icon;
  export const MessageSquare: Icon;
  export const Mic: Icon;
  export const MicOff: Icon;
  export const Minus: Icon;
  export const Monitor: Icon;
  export const MonitorOff: Icon;
  export const Moon: Icon;
  export const MoreHorizontal: Icon;
  export const MoreVertical: Icon;
  export const Music: Icon;
  export const Paperclip: Icon;
  export const Pause: Icon;
  export const Phone: Icon;
  export const PhoneCall: Icon;
  export const PhoneForwarded: Icon;
  export const PhoneIncoming: Icon;
  export const PhoneMissed: Icon;
  export const PhoneOff: Icon;
  export const PhoneOutgoing: Icon;
  export const Pin: Icon;
  export const PinOff: Icon;
  export const Play: Icon;
  export const Plus: Icon;
  export const RefreshCw: Icon;
  export const Repeat: Icon;
  export const Reply: Icon;
  export const Search: Icon;
  export const Send: Icon;
  export const Share2: Icon;
  export const Server: Icon;
  export const Settings: Icon;
  export const Shield: Icon;
  export const Smile: Icon;
  export const Speaker: Icon;
  export const Square: Icon;
  export const Star: Icon;
  export const Sun: Icon;
  export const Trash: Icon;
  export const Trash2: Icon;
  export const Upload: Icon;
  export const User: Icon;
  export const UserCheck: Icon;
  export const UserMinus: Icon;
  export const UserPlus: Icon;
  export const Users: Icon;
  export const Video: Icon;
  export const VideoOff: Icon;
  export const Volume2: Icon;
  export const VolumeX: Icon;
  export const Wifi: Icon;
  export const WifiOff: Icon;
  export const X: Icon;
  export const XCircle: Icon;
  export const ZoomIn: Icon;
  export const ZoomOut: Icon;

  // Additional icons
  export const AlertCircle: Icon;
  export const Ban: Icon;
  export const CheckCheck: Icon;
  export const Maximize2: Icon;
  export const Minimize2: Icon;
  export const Sparkles: Icon;
  export const Smartphone: Icon;
  export const Usb: Icon;
  export const Zap: Icon;
  export const Cable: Icon;

  // Admin panel icons (added for AdminPanel — operator console).
  export const Activity: Icon;
  export const Crown: Icon;
  export const GitBranch: Icon;
  export const HardDrive: Icon;
  export const KeyRound: Icon;
  export const LayoutDashboard: Icon;
  export const Network: Icon;
  export const ScrollText: Icon;
  export const ShieldAlert: Icon;
  export const ShieldCheck: Icon;
  export const ShieldOff: Icon;
  export const Stethoscope: Icon;

  // Public icon component type — used by AdminPanel's TABS array so we can
  // pass the imported icons around as ComponentType without TypeScript
  // complaining about the difference between IconProps and {size?: number}.
  export type LucideIcon = Icon;

  // KeyboardShortcuts modal + others
  export const Keyboard: Icon;
  export const AtSign: Icon;

  // SavedMessagesPage + future bookmark / folder UI
  export const Bookmark: Icon;
  export const Folder: Icon;
  export const Edit2: Icon;
}
