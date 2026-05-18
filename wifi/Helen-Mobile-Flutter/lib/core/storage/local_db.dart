/// Drift (SQLite) database — offline cache for channels, messages, users.
///
/// Codegen note: requires `drift_dev` + `build_runner` to produce
/// `local_db.g.dart`. Run:
///
///   flutter pub run build_runner build --delete-conflicting-outputs
library;

import 'package:drift/drift.dart';
import 'package:drift_flutter/drift_flutter.dart';

part 'local_db.g.dart';

@DataClassName('CachedUser')
class CachedUsers extends Table {
  TextColumn get id => text()();
  TextColumn get username => text()();
  TextColumn get displayName => text()();
  TextColumn get avatarUrl => text().nullable()();
  TextColumn get status => text().withDefault(const Constant('offline'))();
  TextColumn get role => text().withDefault(const Constant('user'))();
  DateTimeColumn get lastSeen => dateTime().nullable()();
  DateTimeColumn get cachedAt => dateTime()();
  @override
  Set<Column<Object>> get primaryKey => <Column<Object>>{id};
}

@DataClassName('CachedChannel')
class CachedChannels extends Table {
  TextColumn get id => text()();
  TextColumn get type => text()();
  TextColumn get name => text().nullable()();
  TextColumn get description => text().nullable()();
  TextColumn get lastMessageId => text().nullable()();
  IntColumn get unreadCount => integer().withDefault(const Constant(0))();
  BoolColumn get isPinned => boolean().withDefault(const Constant(false))();
  BoolColumn get isArchived => boolean().withDefault(const Constant(false))();
  DateTimeColumn get updatedAt => dateTime()();
  DateTimeColumn get cachedAt => dateTime()();
  @override
  Set<Column<Object>> get primaryKey => <Column<Object>>{id};
}

@DataClassName('CachedMessage')
class CachedMessages extends Table {
  TextColumn get id => text()();
  TextColumn get channelId => text()();
  TextColumn get senderId => text()();
  TextColumn get content => text()();
  TextColumn get type => text().withDefault(const Constant('text'))();
  TextColumn get replyTo => text().nullable()();
  TextColumn get fileId => text().nullable()();
  BoolColumn get isPinned => boolean().withDefault(const Constant(false))();
  BoolColumn get isEdited => boolean().withDefault(const Constant(false))();
  BoolColumn get isDeleted => boolean().withDefault(const Constant(false))();
  DateTimeColumn get createdAt => dateTime()();
  DateTimeColumn get cachedAt => dateTime()();
  @override
  Set<Column<Object>> get primaryKey => <Column<Object>>{id};
}

/// Outbound queue — messages typed offline get persisted here and flushed
/// when connectivity returns. Stops keystrokes from vanishing on flaky LAN.
@DataClassName('PendingMessage')
class PendingMessages extends Table {
  IntColumn get localId => integer().autoIncrement()();
  TextColumn get channelId => text()();
  TextColumn get content => text()();
  TextColumn get type => text().withDefault(const Constant('text'))();
  TextColumn get replyTo => text().nullable()();
  IntColumn get attempts => integer().withDefault(const Constant(0))();
  DateTimeColumn get createdAt => dateTime()();
  DateTimeColumn get lastTryAt => dateTime().nullable()();
  TextColumn get lastError => text().nullable()();
}

@DriftDatabase(
  tables: <Type>[CachedUsers, CachedChannels, CachedMessages, PendingMessages],
)
class HelenDb extends _$HelenDb {
  HelenDb() : super(driftDatabase(name: 'helen_db'));
  HelenDb.test(super.e);

  @override
  int get schemaVersion => 1;

  @override
  MigrationStrategy get migration => MigrationStrategy(
        onCreate: (Migrator m) async {
          await m.createAll();
        },
        onUpgrade: (Migrator m, int from, int to) async {
          // Future schema migrations go here.
        },
      );

  // ── Channels ──────────────────────────────────────────────────────

  Future<void> upsertChannel(CachedChannelsCompanion row) =>
      into(cachedChannels).insertOnConflictUpdate(row);

  Future<List<CachedChannel>> getAllChannels() =>
      (select(cachedChannels)..orderBy(<OrderClauseGenerator<$CachedChannelsTable>>[
            (t) => OrderingTerm.desc(t.updatedAt),
          ]))
          .get();

  Stream<List<CachedChannel>> watchChannels() =>
      (select(cachedChannels)..orderBy(<OrderClauseGenerator<$CachedChannelsTable>>[
            (t) => OrderingTerm.desc(t.updatedAt),
          ]))
          .watch();

  // ── Messages ──────────────────────────────────────────────────────

  Future<void> upsertMessage(CachedMessagesCompanion row) =>
      into(cachedMessages).insertOnConflictUpdate(row);

  Future<List<CachedMessage>> getMessages(String channelId, {int limit = 50}) =>
      (select(cachedMessages)
            ..where(($CachedMessagesTable t) => t.channelId.equals(channelId))
            ..orderBy(<OrderClauseGenerator<$CachedMessagesTable>>[
              (t) => OrderingTerm.desc(t.createdAt),
            ])
            ..limit(limit))
          .get();

  Stream<List<CachedMessage>> watchMessages(String channelId, {int limit = 50}) =>
      (select(cachedMessages)
            ..where(($CachedMessagesTable t) => t.channelId.equals(channelId))
            ..orderBy(<OrderClauseGenerator<$CachedMessagesTable>>[
              (t) => OrderingTerm.desc(t.createdAt),
            ])
            ..limit(limit))
          .watch();

  // ── Pending queue ─────────────────────────────────────────────────

  Future<int> enqueuePending(PendingMessagesCompanion row) =>
      into(pendingMessages).insert(row);

  Future<List<PendingMessage>> drainPending({int limit = 20}) =>
      (select(pendingMessages)
            ..orderBy(<OrderClauseGenerator<$PendingMessagesTable>>[
              (t) => OrderingTerm.asc(t.createdAt),
            ])
            ..limit(limit))
          .get();

  Future<void> deletePending(int localId) async {
    await (delete(pendingMessages)
          ..where(($PendingMessagesTable t) => t.localId.equals(localId)))
        .go();
  }

  // ── Wipe (logout) ─────────────────────────────────────────────────

  Future<void> wipe() async {
    await batch((Batch b) {
      b.deleteAll(cachedMessages);
      b.deleteAll(cachedChannels);
      b.deleteAll(cachedUsers);
      b.deleteAll(pendingMessages);
    });
  }
}
