CC ?= gcc
CFLAGS ?= -O2 -shared -fPIC -Wall -Wextra
LDFLAGS ?= -lSDL3 -lGLESv2 -ldl

TARGET = libmocktail_mouse_helper.so
SRC = libmocktail_mouse_helper.c
INSTALL_DIR ?= $(HOME)/.local/lib/mocktail

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(SRC) -o $(TARGET) $(LDFLAGS)

install: $(TARGET)
	mkdir -p $(INSTALL_DIR)
	install -m 755 $(TARGET) $(INSTALL_DIR)/$(TARGET)
	install -m 644 $(SRC) $(INSTALL_DIR)/$(SRC)

clean:
	rm -f $(TARGET)

.PHONY: all install clean
