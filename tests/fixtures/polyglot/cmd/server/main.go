package main

import (
	"fmt"
	st "github.com/acme/app/internal/store"
)

import "os"

// func Ghost() {}
const MaxItems = 10

type Server struct {
	db *st.DB
}

type Handler interface {
	Serve() error
}

func (s *Server) Run() error {
	fmt.Println("not a call()")
	return st.Open(os.Args)
}

func main() {
	NewServer().Run()
}
