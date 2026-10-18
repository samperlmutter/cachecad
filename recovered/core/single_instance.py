import sys
from PySide6.QtCore import QObject, Signal, QCoreApplication, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

class SingleInstance(QObject):
    messageReceived = Signal(str)

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.server = None
        self.is_primary = False
        socket = QLocalSocket()
        socket.connectToServer(self.key)
        if socket.waitForConnected(500):
            socket.write(b'raise-window')
            socket.flush()
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
            self.is_primary = False
            return
        self.server = QLocalServer(self)
        self.server.removeServer(self.key)
        if not self.server.listen(self.key):
            print(f'Could not listen on server {self.key}: {self.server.errorString()}')
            sys.exit(1)
        self.server.newConnection.connect(self._handle_new_connection)
        self.is_primary = True

    def _handle_new_connection(self):
        """Handle messages from secondary instances."""
        socket = self.server.nextPendingConnection()
        if not socket:
            return
        socket.waitForReadyRead(1000)
        data = socket.readAll().data().decode()
        self.messageReceived.emit(data)
        socket.disconnectFromServer()
