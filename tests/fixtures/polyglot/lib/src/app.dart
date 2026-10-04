import 'package:flutter/material.dart';
import 'package:acme/store.dart';
import 'widgets/button.dart';
import 'dart:async';

class App extends StatelessWidget {
  Widget build(BuildContext context) {
    return Button(onTap: () => Store.save());
  }
}

void main() {
  runApp(App());
}
